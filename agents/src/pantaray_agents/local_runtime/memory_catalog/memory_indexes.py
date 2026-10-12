"""The memory an Action or a Suggestion run starts from.

A run is handed the facts and insights indexes, which open with an overview
and link the files that hold the detail, and the situation each Agent
Experience lesson applies to. Everything else it reads when the task needs it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .agent_experience_content import AGENT_EXPERIENCE_INDEX_PATH
from .agent_experience_delta import lesson_situations
from .artifact_workspace import read_artifact_revision_documents
from .connection import open_memory_catalog_connection
from .errors import MemoryCatalogIntegrityError
from .models import MemoryDocument, MemorySource
from .repository import load_latest_active_node_by_source, load_revision
from .resolver import enqueue_memory_repair

FACTS_INDEX_PATH: Final[str] = "facts/index.md"
INSIGHTS_INDEX_PATH: Final[str] = "insights/index.md"
INDEX_OVERVIEW_HEADING: Final[str] = "## Overview"

_ENTRY_PATHS: Final[dict[MemorySource, str]] = {
    "fact": FACTS_INDEX_PATH,
    "long_term_insight": INSIGHTS_INDEX_PATH,
    "agent_experience": AGENT_EXPERIENCE_INDEX_PATH,
}


@dataclass(frozen=True, slots=True)
class MemoryHead:
    source: MemorySource
    revision_id: str
    documents: tuple[MemoryDocument, ...]


class _HeadIntegrityFailure(MemoryCatalogIntegrityError):
    def __init__(self, *, node_id: str, revision_id: str, message: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.revision_id = revision_id


def opens_with_overview(index_text: str) -> bool:
    headings = (line for line in index_text.splitlines() if line.startswith("## "))
    return next(headings, None) == INDEX_OVERVIEW_HEADING


def load_memory_heads(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
) -> tuple[MemoryHead, ...]:
    """The published facts, insights and Agent Experience trees, read together.

    A tree whose files no longer match its revision is queued for repair and
    the read fails, so a run never starts from memory it cannot trust.
    """

    try:
        return _load_heads(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
            user_id=user_id,
        )
    except _HeadIntegrityFailure as exc:
        with open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection:
            with immediate_transaction(connection):
                enqueue_memory_repair(
                    connection=connection,
                    user_id=user_id,
                    node_id=exc.node_id,
                    detected_revision_id=exc.revision_id,
                    reason="revision_integrity",
                )
        raise MemoryCatalogIntegrityError(str(exc)) from exc


def render_memory_indexes(heads: tuple[MemoryHead, ...]) -> str:
    documents = {head.source: head.documents for head in heads}
    sections = [
        _render_index(documents.get("fact", ()), FACTS_INDEX_PATH),
        _render_index(documents.get("long_term_insight", ()), INSIGHTS_INDEX_PATH),
    ]
    situations = lesson_situations(documents.get("agent_experience", ()))
    sections.append(
        "Lessons from past work, each named by the situation it applies to:\n"
        + "\n".join(f"- {situation}" for situation in situations)
        if situations
        else "Lessons from past work: none yet."
    )
    return "\n\n".join(sections)


def _render_index(documents: tuple[MemoryDocument, ...], path: str) -> str:
    text = next(
        (item.content for item in documents if item.source_path == path), ""
    ).strip()
    if not text:
        return f"{path}: none yet."
    note = "" if opens_with_overview(text) else f"{path} has no overview yet.\n"
    return f'{note}<file path="{path}">\n{text}\n</file>'


def _load_heads(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
) -> tuple[MemoryHead, ...]:
    # Design limit: eager document copies avoid a cross-run revision lease. Introduce
    # lease-backed reads if a load's p95 exceeds 1 s or copied content exceeds 32 MiB.
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        connection.execute("BEGIN")
        try:
            heads = tuple(
                head
                for head in (
                    _load_head(
                        connection=connection,
                        artifact_root=artifact_root,
                        user_id=user_id,
                        source=source,
                    )
                    for source in _ENTRY_PATHS
                )
                if head is not None
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
    return heads


def _load_head(
    *,
    connection: sqlite3.Connection,
    artifact_root: Path,
    user_id: str,
    source: MemorySource,
) -> MemoryHead | None:
    node = load_latest_active_node_by_source(
        connection=connection, user_id=user_id, source=source
    )
    if node is None or node.current_revision_id is None:
        return None
    revision = load_revision(
        connection=connection, user_id=user_id, revision_id=node.current_revision_id
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
        raise _HeadIntegrityFailure(
            node_id=node.node_id, revision_id=revision.revision_id, message=str(exc)
        ) from exc
    if not any(item.source_path == _ENTRY_PATHS[source] for item in documents):
        raise _HeadIntegrityFailure(
            node_id=node.node_id,
            revision_id=revision.revision_id,
            message=f"{source} memory entry document is absent",
        )
    return MemoryHead(
        source=source, revision_id=revision.revision_id, documents=documents
    )


__all__ = [
    "FACTS_INDEX_PATH",
    "INDEX_OVERVIEW_HEADING",
    "INSIGHTS_INDEX_PATH",
    "MemoryHead",
    "load_memory_heads",
    "opens_with_overview",
    "render_memory_indexes",
]
