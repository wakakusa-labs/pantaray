from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_reader import SourceReader
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
from pantaray_agents.tools.web.session import WebResearchToolSession

from ..action_session_temp_paths import (
    resolve_local_runtime_storage_base,
    validate_action_storage_component,
)
from ..outside_workspace_grant import app_owned_roots
from .commands import SuggestionCommandSession
from .memory_sql import SuggestionMemorySqlSession
from .snapshot import SuggestionResearchSnapshot
from .zanei import InsightActivityStart, SuggestionZaneiSession

# The Action's memory_search snippet limit: memory is read through the memory
# tools only, so a result carries the passage rather than a preview of it.
MEMORY_SEARCH_CONTENT_MAX_CHARS = 6_000
MEMORY_SEARCH_MAX_RESULTS = 8
MEMORY_REFERENCE_CONTENT_MAX_CHARS = 4_000
SUGGESTION_TOOL_RESULTS_DIRNAME = "suggestion_tool_results"
READ_IMAGE_NOT_SUPPORTED = "READ_IMAGE_NOT_SUPPORTED"


def suggestion_tool_results_root(*, db_path: Path, run_id: str) -> Path:
    """Where one Suggestion run keeps tool results too large to show inline."""

    validate_action_storage_component(field_name="run_id", value=run_id)
    return (
        resolve_local_runtime_storage_base(db_path=db_path)
        / SUGGESTION_TOOL_RESULTS_DIRNAME
        / run_id
    )


def discard_suggestion_tool_results(*, db_path: Path, run_id: str) -> None:
    """Remove a finished run's large tool results, which only that run reads."""

    # Design limit: a worker that dies mid-run leaves its folder behind; sweep
    # this root at startup if leftovers are seen to grow.
    try:
        shutil.rmtree(suggestion_tool_results_root(db_path=db_path, run_id=run_id))
    except FileNotFoundError:
        return


@dataclass(frozen=True, slots=True)
class LocalSuggestionResearchTools:
    """Compose run-bound common tools available to SuggestionAgent."""

    db_path: Path
    busy_timeout_ms: int
    snapshot: SuggestionResearchSnapshot
    activity_start: InsightActivityStart | None

    def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]:
        memory_context = MemoryContextSession(user_id=user_id, run_id=run_id)
        memory_tools = MemoryRetrievalSession(
            db_path=self.db_path,
            busy_timeout_ms=self.busy_timeout_ms,
            context=memory_context,
            policy=MemoryRetrievalPolicy(
                allowed_focuses=(
                    "stable_knowledge",
                    "activity",
                    "agent_work",
                    "all",
                ),
                default_focus=None,
                max_results=MEMORY_SEARCH_MAX_RESULTS,
                default_limit=None,
                call_limit=None,
                pinned_revisions=self.snapshot.memory_revisions,
                search_content_max_chars=MEMORY_SEARCH_CONTENT_MAX_CHARS,
                reference_content_max_chars=MEMORY_REFERENCE_CONTENT_MAX_CHARS,
                enqueue_repair_on_reference_failure=False,
            ),
        )
        return (
            *memory_tools.definitions(),
            SuggestionMemorySqlSession(
                db_path=self.db_path,
                busy_timeout_ms=self.busy_timeout_ms,
                user_id=user_id,
            ).definition(),
            *(
                _without_images(tool)
                for tool in build_read_only_file_tools(
                    folders=self.snapshot.folders,
                    read_access_scope=self.snapshot.read_access_scope,
                    app_storage_roots=app_owned_roots(self.db_path),
                    spill_root=suggestion_tool_results_root(
                        db_path=self.db_path, run_id=run_id
                    ),
                )
            ),
            *WebResearchToolSession(user_id=user_id).definitions(),
            *SuggestionZaneiSession(
                reader=SourceReader(context_source_control.gate),
                start=self.activity_start,
            ).definitions(),
            *(
                SuggestionCommandSession(
                    db_path=self.db_path,
                    busy_timeout_ms=self.busy_timeout_ms,
                    user_id=user_id,
                    workspace_roots=self.snapshot.folders,
                    read_access_scope=self.snapshot.read_access_scope,
                ).definitions()
                if self.snapshot.commands_allowed
                else ()
            ),
        )


def _without_images(tool: ReactToolDefinition) -> ReactToolDefinition:
    """The tool, refusing an image: Suggestion's model call carries no files."""

    async def execute(call: ReactToolCall, step_number: int) -> ReactToolResult:
        result = await tool.execute(call, step_number)
        if not result.images:
            return result
        return tool_error_response(
            tool_name=call.tool_name,
            error_code=READ_IMAGE_NOT_SUPPORTED,
            message=(
                f"{result.images[0].display_path} is an image "
                f"({result.images[0].mime_type}); this run reads text only and "
                "cannot view it."
            ),
        )

    return replace(tool, execute=execute)


__all__ = [
    "LocalSuggestionResearchTools",
    "discard_suggestion_tool_results",
    "suggestion_tool_results_root",
]
