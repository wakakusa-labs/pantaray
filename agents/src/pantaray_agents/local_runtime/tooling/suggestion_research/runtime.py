from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pantaray_agents.agents.suggestion_agent.research import SuggestionMakeTools
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.local_runtime.integrations.search import (
    integration_tools_available,
)
from pantaray_agents.tools.contract import ReactToolDefinition
from pantaray_agents.tools.files.read_only_tools import read_only_file_tools
from pantaray_agents.tools.memory.retrieval import (
    MemoryContextSession,
    MemoryRetrievalPolicy,
    MemoryRetrievalSession,
)
from pantaray_agents.tools.memory.sql_tool import MemorySqlSession
from pantaray_agents.tools.web.session import WebResearchToolSession

from ..action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    resolve_local_runtime_storage_base,
    resolve_suggestion_artifacts_root,
    validate_action_storage_component,
)
from ..outside_workspace_grant import app_owned_roots
from ..tool_result_storage import discard_run_tool_results
from .artifact_patch import SuggestionArtifactPatch
from .commands import PinnedFolder, SuggestionCommandSession
from .html_pages import SuggestionHtmlPages
from .integrations import IntegrationRunBudget, SuggestionIntegrationSession
from .snapshot import SuggestionResearchSnapshot
from .zanei import InsightActivityStart, SuggestionZaneiSession

# The Action's memory_search snippet limit: memory is read through the memory
# tools only, so a result carries the passage rather than a preview of it.
MEMORY_SEARCH_CONTENT_MAX_CHARS = 6_000
MEMORY_SEARCH_MAX_RESULTS = 8
MEMORY_REFERENCE_CONTENT_MAX_CHARS = 4_000
SUGGESTION_TOOL_RESULTS_DIRNAME = "suggestion_tool_results"


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

    discard_run_tool_results(
        suggestion_tool_results_root(db_path=db_path, run_id=run_id)
    )


@dataclass(frozen=True, slots=True)
class LocalSuggestionResearchTools:
    """Compose run-bound common tools available to SuggestionAgent.

    One instance serves one Suggestion, so its lens runs share the integration
    call budget.
    """

    db_path: Path
    busy_timeout_ms: int
    snapshot: SuggestionResearchSnapshot
    activity_start: InsightActivityStart | None
    integration_runs: IntegrationRunBudget = field(
        default_factory=IntegrationRunBudget, init=False
    )

    async def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]:
        return await self._definitions(
            user_id=user_id, run_id=run_id, artifact_folder=None
        )

    async def build_make_tools(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> SuggestionMakeTools:
        """The research tools, with commands, run_python, apply_patch and
        save_html_as_pdf writing only into the Suggestion's artifact folder,
        and render_html_page to look at a page."""

        folder = resolve_suggestion_artifacts_root(
            db_path=self.db_path, user_id=user_id, suggestion_id=run_id
        )
        folder.mkdir(mode=MANAGED_DIRECTORY_MODE, parents=True, exist_ok=True)
        # Pinned once: no later call resolves the folder from its path again.
        pinned = PinnedFolder.pin(folder.resolve(strict=True))
        definitions = await self._definitions(
            user_id=user_id, run_id=run_id, artifact_folder=pinned
        )
        return SuggestionMakeTools(
            definitions=(
                *definitions,
                SuggestionArtifactPatch(pinned.path).definition(),
            ),
            folder=pinned.path,
        )

    async def _definitions(
        self, *, user_id: str, run_id: str, artifact_folder: PinnedFolder | None
    ) -> tuple[ReactToolDefinition, ...]:
        memory_context = MemoryContextSession(user_id=user_id, run_id=run_id)
        spill_root = suggestion_tool_results_root(db_path=self.db_path, run_id=run_id)
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
        files = read_only_file_tools(
            db_path=self.db_path,
            folders=self.snapshot.folders,
            read_access_scope=self.snapshot.read_access_scope,
            app_storage_roots=app_owned_roots(self.db_path),
            spill_root=spill_root,
            memory_folders=self.snapshot.memory_folders,
            own_folder=None if artifact_folder is None else artifact_folder.path,
        )
        return (
            *memory_tools.definitions(),
            MemorySqlSession(
                db_path=self.db_path,
                busy_timeout_ms=self.busy_timeout_ms,
                user_id=user_id,
            ).definition(),
            *files.definitions(),
            *(
                ()
                if artifact_folder is None
                else SuggestionHtmlPages(
                    files=files,
                    folder=artifact_folder,
                    user_id=user_id,
                    run_id=run_id,
                ).definitions()
            ),
            *WebResearchToolSession(
                user_id=user_id,
                speaks_to_user=False,
                spill_root=spill_root,
            ).definitions(),
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
                    spill_root=spill_root,
                    artifact_folder=artifact_folder,
                ).definitions()
                if self.snapshot.commands_allowed
                else ()
            ),
            # Decided when each lens run starts, as an Action decides when it starts.
            *(
                SuggestionIntegrationSession(
                    db_path=self.db_path,
                    busy_timeout_ms=self.busy_timeout_ms,
                    user_id=user_id,
                    budget=self.integration_runs,
                    spill_root=spill_root,
                ).definitions()
                if await integration_tools_available()
                else ()
            ),
        )


__all__ = [
    "LocalSuggestionResearchTools",
    "discard_suggestion_tool_results",
    "suggestion_tool_results_root",
]
