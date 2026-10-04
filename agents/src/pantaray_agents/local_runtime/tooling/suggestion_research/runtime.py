from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.artifact_react import ReactToolDefinition
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MemoryContextSession,
    MemoryRetrievalPolicy,
    MemoryRetrievalSession,
)
from pantaray_agents.local_runtime.tooling.react_tools import (
    ReadOnlyFileToolSession,
    WebResearchToolSession,
    WorkspaceReadRoot,
    memory_revision_by_source,
)

from .commands import SuggestionCommandSession
from .memory_sql import SuggestionMemorySqlSession
from .snapshot import SuggestionResearchSnapshot
from .zanei import InsightActivityStart, SuggestionZaneiSession

MEMORY_SEARCH_CONTENT_MAX_CHARS = 500
MEMORY_SEARCH_MAX_RESULTS = 8
MEMORY_REFERENCE_CONTENT_MAX_CHARS = 4_000


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
                pinned_revisions=memory_revision_by_source(self.snapshot.roots),
                search_content_max_chars=MEMORY_SEARCH_CONTENT_MAX_CHARS,
                reference_content_max_chars=MEMORY_REFERENCE_CONTENT_MAX_CHARS,
                enqueue_repair_on_reference_failure=False,
                full_read_tools_available=True,
            ),
        )
        return (
            *memory_tools.definitions(),
            SuggestionMemorySqlSession(
                db_path=self.db_path,
                busy_timeout_ms=self.busy_timeout_ms,
                user_id=user_id,
            ).definition(),
            *ReadOnlyFileToolSession(
                roots=self.snapshot.roots,
                user_id=user_id,
                memory_context=memory_context,
            ).definitions(),
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
                    workspace_roots=tuple(
                        root.canonical_path
                        for root in self.snapshot.roots
                        if isinstance(root, WorkspaceReadRoot)
                    ),
                    read_access_scope=self.snapshot.read_access_scope,
                ).definitions()
                if self.snapshot.commands_allowed
                else ()
            ),
        )


__all__ = ["LocalSuggestionResearchTools"]
