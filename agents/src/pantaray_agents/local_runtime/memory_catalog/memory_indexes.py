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
from .revision_inspection import REVISION_INTEGRITY_ERRORS

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


@dataclass(frozen=True, slots=True)
class MemoryHeads:
    heads: tuple[MemoryHead, ...]
    # Published heads whose files no longer match their revision; each has been
    # queued for repair.
    unreadable: tuple[MemorySource, ...]


@dataclass(frozen=True, slots=True)
class _HeadIntegrityFailure:
    source: MemorySource
    node_id: str
    revision_id: str


def opens_with_overview(index_text: str) -> bool:
    headings = (line for line in index_text.splitlines() if line.startswith("## "))
    return next(headings, None) == INDEX_OVERVIEW_HEADING


def load_memory_heads(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    user_id: str,
) -> MemoryHeads:
    """The published facts, insights and Agent Experience trees, read together.

    A tree whose files no longer match its revision is queued for repair and
    reported as unreadable, so the other trees still reach the run.
    """

    heads, failures = _load_heads(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        user_id=user_id,
    )
    if failures:
        with open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection:
            with immediate_transaction(connection):
                for failure in failures:
                    enqueue_memory_repair(
                        connection=connection,
                        user_id=user_id,
                        node_id=failure.node_id,
                        detected_revision_id=failure.revision_id,
                        reason="revision_integrity",
                    )
    return MemoryHeads(
        heads=heads, unreadable=tuple(failure.source for failure in failures)
    )


def render_memory_indexes(memory: MemoryHeads) -> str:
    documents = {head.source: head.documents for head in memory.heads}

    def index(source: MemorySource) -> str:
        path = _ENTRY_PATHS[source]
        if source in memory.unreadable:
            return f"{path}: could not be read."
        return _render_index(documents.get(source, ()), path)

    sections = [index("fact"), index("long_term_insight")]
    situations = lesson_situations(documents.get("agent_experience", ()))
    if "agent_experience" in memory.unreadable:
        sections.append("Lessons from past work: could not be read.")
    elif situations:
        sections.append(
            "Lessons from past work, each named by the situation it applies to:\n"
            + "\n".join(f"- {situation}" for situation in situations)
        )
    else:
        sections.append("Lessons from past work: none yet.")
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
) -> tuple[tuple[MemoryHead, ...], tuple[_HeadIntegrityFailure, ...]]:
    # Design limit: eager document copies avoid a cross-run revision lease. Introduce
    # lease-backed reads if a load's p95 exceeds 1 s or copied content exceeds 32 MiB.
    heads: list[MemoryHead] = []
    failures: list[_HeadIntegrityFailure] = []
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        connection.execute("BEGIN")
        try:
            for source in _ENTRY_PATHS:
                loaded = _load_head(
                    connection=connection,
                    artifact_root=artifact_root,
                    user_id=user_id,
                    source=source,
                )
                if isinstance(loaded, MemoryHead):
                    heads.append(loaded)
                elif loaded is not None:
                    failures.append(loaded)
        except Exception:
            connection.rollback()
            raise
        connection.commit()
    return tuple(heads), tuple(failures)


def _load_head(
    *,
    connection: sqlite3.Connection,
    artifact_root: Path,
    user_id: str,
    source: MemorySource,
) -> MemoryHead | _HeadIntegrityFailure | None:
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
    failure = _HeadIntegrityFailure(
        source=source, node_id=node.node_id, revision_id=revision.revision_id
    )
    try:
        documents = read_artifact_revision_documents(
            connection=connection,
            artifact_root=artifact_root,
            user_id=user_id,
            revision_id=revision.revision_id,
        )
    except REVISION_INTEGRITY_ERRORS:
        return failure
    if not any(item.source_path == _ENTRY_PATHS[source] for item in documents):
        return failure
    return MemoryHead(
        source=source, revision_id=revision.revision_id, documents=documents
    )


__all__ = [
    "FACTS_INDEX_PATH",
    "INDEX_OVERVIEW_HEADING",
    "INSIGHTS_INDEX_PATH",
    "MemoryHead",
    "MemoryHeads",
    "load_memory_heads",
    "opens_with_overview",
    "render_memory_indexes",
]
