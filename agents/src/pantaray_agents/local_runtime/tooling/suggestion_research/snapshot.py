from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.memory_catalog.agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
)
from pantaray_agents.local_runtime.memory_catalog.artifact_workspace import (
    read_artifact_revision_documents,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.document_rendering import (
    TODO_DOCUMENT_PATH,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    load_latest_active_node_by_source,
    load_revision,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import enqueue_memory_repair
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.react_tools import (
    MemoryReadRoot,
    ReadOnlyRoot,
    WorkspaceReadRoot,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.schema.read_access import ReadAccessScope

from ..outside_workspace_grant import app_owned_roots
from .commands import commands_run_without_asking

# The prompt carries insights/todos.md up to this size (production peaked at 41k
# characters); a larger file is cut with a marker and the run reads the rest.
PENDING_WORK_MAX_CHARS = 60_000
STABLE_MEMORY_CONTEXT_MAX_CHARS = 4_500
STABLE_MEMORY_ITEM_MAX_CHARS = 650
STABLE_MEMORY_TREE_MAX_CHARS = 450
STABLE_MEMORY_TREE_MAX_PATHS = 30


@dataclass(frozen=True, slots=True)
class SuggestionResearchSnapshot:
    roots: tuple[ReadOnlyRoot, ...]
    stable_memory: SuggestionStableMemoryContext
    # Whether the run offers commands at all; each command checks again.
    commands_allowed: bool
    read_access_scope: ReadAccessScope


@dataclass(frozen=True, slots=True)
class _LoadedMemoryRoot:
    root: MemoryReadRoot
    profile_brief: str


class _SnapshotIntegrityFailure(MemoryCatalogIntegrityError):
    def __init__(self, *, node_id: str, revision_id: str, message: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.revision_id = revision_id


def build_suggestion_research_snapshot(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
    workspace_settings: WorkspaceSettings,
) -> SuggestionResearchSnapshot:
    workspace_roots = _workspace_roots(workspace_settings, db_path=db_path)
    try:
        loaded_memory = _load_memory_roots(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
            user_id=user_id,
        )
    except _SnapshotIntegrityFailure as exc:
        _enqueue_snapshot_repair(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            failure=exc,
        )
        raise MemoryCatalogIntegrityError(str(exc)) from exc

    memory_roots = tuple(item.root for item in loaded_memory)
    roots: tuple[ReadOnlyRoot, ...] = (*memory_roots, *workspace_roots)
    stable_memory = SuggestionStableMemoryContext(
        prompt=_render_stable_memory_prompt(
            loaded_memory=loaded_memory,
            roots=roots,
        ),
        has_facts=any(
            root.root_id == "facts"
            and any(doc.content.strip() for doc in root.documents)
            for root in memory_roots
        ),
        has_insights=any(
            root.root_id == "insights"
            and any(
                doc.source_path != TODO_DOCUMENT_PATH and doc.content.strip()
                for doc in root.documents
            )
            for root in memory_roots
        ),
        pending_work=_bounded(
            next(
                (
                    doc.content
                    for root in memory_roots
                    if root.root_id == "insights"
                    for doc in root.documents
                    if doc.source_path == TODO_DOCUMENT_PATH
                ),
                "",
            ),
            limit=PENDING_WORK_MAX_CHARS,
        ),
    )
    return SuggestionResearchSnapshot(
        roots=roots,
        stable_memory=stable_memory,
        commands_allowed=commands_run_without_asking(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
        read_access_scope=workspace_settings.read_access_scope,
    )


def _workspace_roots(
    settings: WorkspaceSettings, *, db_path: Path
) -> tuple[WorkspaceReadRoot, ...]:
    private_app_storage = app_owned_roots(db_path)
    roots = tuple(
        WorkspaceReadRoot(
            root_id=f"workspace:{folder.folder_id}",
            display_name=folder.display_name,
            canonical_path=Path(folder.canonical_real_path),
            private_app_storage=private_app_storage,
        )
        for folder in settings.folders
    )
    root_ids = [root.root_id for root in roots]
    if len(root_ids) != len(set(root_ids)):
        raise ValueError("Suggestion readable root ids must be unique")
    return roots


def _load_memory_roots(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
) -> tuple[_LoadedMemoryRoot, ...]:
    # Design limit: eager document copies avoid a cross-run revision lease. Introduce
    # lease-backed reads if snapshot p95 exceeds 1 s or copied content exceeds 32 MiB/run.
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN")
        try:
            loaded = tuple(
                item
                for item in (
                    _load_memory_root(
                        connection=connection,
                        artifact_root=artifact_root,
                        user_id=user_id,
                        source="fact",
                        root_id="facts",
                        display_name="Structured Facts",
                        entry_path="facts/index.md",
                    ),
                    _load_memory_root(
                        connection=connection,
                        artifact_root=artifact_root,
                        user_id=user_id,
                        source="long_term_insight",
                        root_id="insights",
                        display_name="Long-term Insights",
                        entry_path="insights/index.md",
                    ),
                    _load_memory_root(
                        connection=connection,
                        artifact_root=artifact_root,
                        user_id=user_id,
                        source="agent_experience",
                        root_id="agent_experience",
                        display_name="Agent Experience",
                        entry_path=AGENT_EXPERIENCE_INDEX_PATH,
                    ),
                )
                if item is not None
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
    return loaded


def _load_memory_root(
    *,
    connection: sqlite3.Connection,
    artifact_root: Path,
    user_id: str,
    source: MemorySource,
    root_id: str,
    display_name: str,
    entry_path: str,
) -> _LoadedMemoryRoot | None:
    node = load_latest_active_node_by_source(
        connection=connection,
        user_id=user_id,
        source=source,
    )
    if node is None or node.current_revision_id is None:
        return None
    revision = load_revision(
        connection=connection,
        user_id=user_id,
        revision_id=node.current_revision_id,
    )
    if revision is None:
        raise MemoryCatalogIntegrityError(
            f"{source} memory head has no current revision"
        )
    try:
        documents = read_artifact_revision_documents(
            connection=connection,
            artifact_root=artifact_root,
            user_id=user_id,
            revision_id=revision.revision_id,
        )
    except (MemoryCatalogIntegrityError, UnicodeError) as exc:
        raise _SnapshotIntegrityFailure(
            node_id=node.node_id,
            revision_id=revision.revision_id,
            message=str(exc),
        ) from exc
    if not any(document.source_path == entry_path for document in documents):
        raise _SnapshotIntegrityFailure(
            node_id=node.node_id,
            revision_id=revision.revision_id,
            message=f"{source} memory entry document is absent",
        )
    return _LoadedMemoryRoot(
        root=MemoryReadRoot(
            root_id=root_id,
            display_name=display_name,
            revision_id=revision.revision_id,
            node_id=node.node_id,
            entry_path=entry_path,
            documents=documents,
        ),
        profile_brief=(revision.profile_brief or "").strip(),
    )


def _enqueue_snapshot_repair(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    failure: _SnapshotIntegrityFailure,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as connection:
        with immediate_transaction(connection):
            enqueue_memory_repair(
                connection=connection,
                user_id=user_id,
                node_id=failure.node_id,
                detected_revision_id=failure.revision_id,
                reason="revision_integrity",
            )


def _render_stable_memory_prompt(
    *,
    loaded_memory: tuple[_LoadedMemoryRoot, ...],
    roots: tuple[ReadOnlyRoot, ...],
) -> str:
    sections = [
        "Stable memory is a run-start snapshot. Use read/list/glob/grep for details.",
        "\n### Readable roots",
    ]
    if not roots:
        sections.append("- None")
    for root in roots:
        if isinstance(root, MemoryReadRoot):
            sections.append(
                f"- {root.root_id}: {root.display_name}; entry={root.entry_path}"
            )
        else:
            sections.append(f"- {root.root_id}: {root.display_name}")
    for item in loaded_memory:
        index = _document_content(item.root.documents, item.root.entry_path)
        sections.extend(
            (
                f"\n### {item.root.display_name}",
                f"Root: {item.root.root_id}; entry: {item.root.entry_path}",
                "File tree:\n" + _render_document_tree(item.root.documents),
            )
        )
        if item.profile_brief:
            sections.append("Profile brief:\n" + _bounded(item.profile_brief))
        sections.append("Index preview:\n" + _bounded(index))
    return _bounded("\n".join(sections), limit=STABLE_MEMORY_CONTEXT_MAX_CHARS)


def _document_content(documents: tuple[MemoryDocument, ...], path: str) -> str:
    return next(
        document.content for document in documents if document.source_path == path
    )


def _render_document_tree(documents: tuple[MemoryDocument, ...]) -> str:
    marker = "- [truncated; continue with list]"
    ordered = sorted(documents, key=lambda item: item.source_path)
    selected = [
        f"- {document.source_path}"
        for document in ordered[:STABLE_MEMORY_TREE_MAX_PATHS]
    ]
    truncated = len(selected) < len(ordered)
    while selected and len("\n".join(selected)) > STABLE_MEMORY_TREE_MAX_CHARS:
        selected.pop()
        truncated = True
    if truncated:
        while selected and len("\n".join((*selected, marker))) > (
            STABLE_MEMORY_TREE_MAX_CHARS
        ):
            selected.pop()
        selected.append(marker)
    return "\n".join(selected)


def _bounded(value: str, *, limit: int = STABLE_MEMORY_ITEM_MAX_CHARS) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    marker = "\n[truncated; continue with read]"
    if limit <= len(marker):
        return marker[:limit]
    content_limit = limit - len(marker)
    return stripped[:content_limit].rstrip() + marker


__all__ = [
    "SuggestionResearchSnapshot",
    "build_suggestion_research_snapshot",
]
