from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.embedding_local import (
    LocalEmbeddingUnavailableError,
    load_local_embedding_model,
)
from pantaray_agents.schema.memory_embeddings import MemorySearchSemanticStatus

from .connection import open_memory_catalog_connection
from .context_search import (
    SemanticSearchInput,
    merge_memory_search_context,
    search_memory_catalog,
)
from .embedding_generations import (
    EmbeddingGeneration,
    current_embedding_specification,
    load_active_embedding_generation,
)
from .lexical_search import lexical_query_notes
from .models import MemoryContextEpoch, MemorySearchResult, MemorySource
from .search_policy import MemorySearchFocus

logger = logging.getLogger(__name__)

# Reported next to `provider_unavailable` so an operator can tell a missing
# embedding model from one that is installed but failed while embedding.
MEMORY_EMBEDDING_MODEL_UNAVAILABLE = "MEMORY_EMBEDDING_MODEL_UNAVAILABLE"
MEMORY_EMBEDDING_QUERY_FAILED = "MEMORY_EMBEDDING_QUERY_FAILED"
# Distinguishes an index that is still being built from one that cannot finish.
MEMORY_EMBEDDING_INDEX_BLOCKED = "MEMORY_EMBEDDING_INDEX_BLOCKED"


@dataclass(frozen=True, slots=True)
class MemorySearchRequest:
    """At least one of keywords and query; keywords are stripped and distinct."""

    user_id: str
    run_id: str
    keywords: tuple[str, ...]
    query: str | None
    focus: MemorySearchFocus
    limit: int
    center_time: str | None = None
    radius_hours: int | None = None
    pinned_revisions: Mapping[MemorySource, str | None] | None = None
    current_epoch: MemoryContextEpoch | None = None


@dataclass(frozen=True, slots=True)
class MemorySearchResponse:
    results: tuple[MemorySearchResult, ...]
    epoch: MemoryContextEpoch
    semantic_status: MemorySearchSemanticStatus
    semantic_error_code: str | None = None
    # What the search left out of the query, in sentences the model reads.
    notes: tuple[str, ...] = ()


def memory_search_notes(
    response: MemorySearchResponse, *, limit: int, max_limit: int
) -> list[str]:
    """The response's notes, plus one when the result list is full."""

    notes = list(response.notes)
    if len(response.results) >= limit:
        larger = (
            f"pass a larger limit (up to {max_limit}) or " if limit < max_limit else ""
        )
        notes.append(
            f"The results stop at limit={limit}; more may match. To reach "
            f"others, {larger}search with more specific terms."
        )
    return notes


async def execute_memory_search(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: MemorySearchRequest,
) -> MemorySearchResponse:
    # Reading the catalog and embedding the query take seconds on a large
    # store; on the event loop they would stall every other run in the process.
    return await asyncio.to_thread(
        _search, db_path=db_path, busy_timeout_ms=busy_timeout_ms, request=request
    )


def _search(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    request: MemorySearchRequest,
) -> MemorySearchResponse:
    selected_generation = _load_selected_active_generation(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=request.user_id,
    )
    semantic_status: MemorySearchSemanticStatus = "not_ready"
    semantic_error_code: str | None = None
    vectors: tuple[tuple[float, ...] | None, tuple[float, ...] | None] | None = None
    try:
        model = load_local_embedding_model()
    except LocalEmbeddingUnavailableError:
        semantic_status = "provider_unavailable"
        semantic_error_code = MEMORY_EMBEDDING_MODEL_UNAVAILABLE
    else:
        # A generation built by a different artifact cannot be searched with
        # this model's vectors; its replacement is still being built, which is
        # what `not_ready` means.
        if selected_generation is not None and selected_generation.specification == (
            current_embedding_specification(model.manifest)
        ):
            try:
                vectors = (
                    model.embed_query(" ".join(request.keywords))
                    if request.keywords
                    else None,
                    model.embed_query(request.query) if request.query else None,
                )
            # The model is installed but could not embed this query. Reporting
            # it keeps the lexical and exact lanes answering instead of failing
            # the whole search.
            except Exception:
                logger.warning("memory search could not embed the query", exc_info=True)
                semantic_status = "provider_unavailable"
                semantic_error_code = MEMORY_EMBEDDING_QUERY_FAILED
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN")
        with connection:
            generation = load_active_embedding_generation(
                connection,
                user_id=request.user_id,
            )
            semantic: SemanticSearchInput | None = None
            if vectors is None:
                if semantic_status == "not_ready":
                    semantic_error_code = _not_ready_reason(
                        connection, user_id=request.user_id
                    )
            elif generation != selected_generation or generation is None:
                semantic_status = "generation_changed"
                semantic_error_code = None
            else:
                semantic = SemanticSearchInput(
                    generation=generation,
                    keywords_embedding=vectors[0],
                    query_embedding=vectors[1],
                )
                semantic_status = "available"
            results, search_epoch = search_memory_catalog(
                connection=connection,
                user_id=request.user_id,
                run_id=request.run_id,
                keywords=request.keywords,
                semantic=semantic,
                focus=request.focus,
                center_time=request.center_time,
                radius_hours=request.radius_hours,
                limit=request.limit,
                pinned_revisions=request.pinned_revisions,
            )
            results, epoch = merge_memory_search_context(
                results=results,
                current_epoch=request.current_epoch,
                search_epoch=search_epoch,
            )
    return MemorySearchResponse(
        results=tuple(results),
        epoch=epoch,
        semantic_status=semantic_status,
        semantic_error_code=semantic_error_code,
        notes=_search_notes(request, semantic_status),
    )


def _search_notes(
    request: MemorySearchRequest, semantic_status: MemorySearchSemanticStatus
) -> tuple[str, ...]:
    notes = lexical_query_notes(" ".join(request.keywords))
    if request.query and semantic_status != "available":
        # query is matched by meaning only, so it found nothing this time.
        notes += (
            f"query was not searched (semantic_status={semantic_status}); put "
            "the names and words to match in keywords.",
        )
    return notes


def _not_ready_reason(connection: sqlite3.Connection, *, user_id: str) -> str | None:
    """Whether the index is still being built or cannot finish being built.

    Both look identical to a caller otherwise, and only one of them will ever
    resolve on its own.
    """
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM memory_embedding_work AS work
            JOIN memory_embedding_user_generations AS scopes
              ON scopes.user_id = work.user_id
             AND scopes.generation_id = work.generation_id
            WHERE work.user_id = ? AND scopes.state = 'building'
              AND work.state = 'failed'
        )
        """,
        (user_id,),
    ).fetchone()
    return MEMORY_EMBEDDING_INDEX_BLOCKED if row is not None and row[0] else None


def _load_selected_active_generation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> EmbeddingGeneration | None:
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as connection:
        generation = load_active_embedding_generation(connection, user_id=user_id)
    return generation


__all__ = [
    "MEMORY_EMBEDDING_INDEX_BLOCKED",
    "MEMORY_EMBEDDING_MODEL_UNAVAILABLE",
    "MEMORY_EMBEDDING_QUERY_FAILED",
    "MemorySearchRequest",
    "MemorySearchResponse",
    "execute_memory_search",
    "memory_search_notes",
]
