from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.memory_catalog.document_rendering import (
    TODO_DOCUMENT_PATH,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.memory_indexes import (
    load_memory_heads,
    render_memory_indexes,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.schema.read_access import ReadAccessScope

from .commands import commands_run_without_asking

# The prompt carries insights/todos.md up to this size (production peaked at 41k
# characters); a larger file is cut with a marker pointing to memory_search.
PENDING_WORK_MAX_CHARS = 60_000


@dataclass(frozen=True, slots=True)
class SuggestionResearchSnapshot:
    # The registered folders' canonical paths, which the file tools may read.
    folders: tuple[Path, ...]
    # The memory revisions the run's memory_search stays on.
    memory_revisions: Mapping[MemorySource, str | None]
    stable_memory: SuggestionStableMemoryContext
    # Whether the run offers commands at all; each command checks again.
    commands_allowed: bool
    read_access_scope: ReadAccessScope


def build_suggestion_research_snapshot(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
    workspace_settings: WorkspaceSettings,
) -> SuggestionResearchSnapshot:
    memory = load_memory_heads(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        user_id=user_id,
    )
    if memory.unreadable:
        # The repair is queued; a Suggestion does not start from memory it cannot trust.
        raise MemoryCatalogIntegrityError(
            f"unreadable memory heads: {', '.join(memory.unreadable)}"
        )
    heads = memory.heads
    stable_memory = SuggestionStableMemoryContext(
        prompt=render_memory_indexes(memory),
        has_facts=any(
            head.source == "fact" and any(doc.content.strip() for doc in head.documents)
            for head in heads
        ),
        has_insights=any(
            head.source == "long_term_insight"
            and any(
                doc.source_path != TODO_DOCUMENT_PATH and doc.content.strip()
                for doc in head.documents
            )
            for head in heads
        ),
        pending_work=_bounded(
            next(
                (
                    doc.content
                    for head in heads
                    if head.source == "long_term_insight"
                    for doc in head.documents
                    if doc.source_path == TODO_DOCUMENT_PATH
                ),
                "",
            ),
            limit=PENDING_WORK_MAX_CHARS,
        ),
    )
    # A source without a published head is pinned to None, which hides any
    # revision published during the run; a missing key would show it.
    memory_revisions: dict[MemorySource, str | None] = {
        "fact": None,
        "long_term_insight": None,
        "agent_experience": None,
    }
    memory_revisions.update((head.source, head.revision_id) for head in heads)
    return SuggestionResearchSnapshot(
        folders=tuple(
            Path(folder.canonical_real_path) for folder in workspace_settings.folders
        ),
        memory_revisions=memory_revisions,
        stable_memory=stable_memory,
        commands_allowed=commands_run_without_asking(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
        read_access_scope=workspace_settings.read_access_scope,
    )


def _bounded(value: str, *, limit: int) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    marker = "\n[truncated; find the rest with memory_search]"
    return stripped[: limit - len(marker)].rstrip() + marker


__all__ = [
    "SuggestionResearchSnapshot",
    "build_suggestion_research_snapshot",
]
