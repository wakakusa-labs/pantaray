from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.embedding_local import (
    LocalEmbeddingUnavailableError,
)
from pantaray_agents.local_runtime.memory_catalog import search_service
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    EmbeddingGeneration,
    activate_embedding_generation_in_transaction,
    current_embedding_specification,
    ensure_user_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemoryReferenceDepth,
    MemorySearchResult,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MEMORY_EMBEDDING_MODEL_UNAVAILABLE,
    MemorySearchRequest,
    execute_memory_search,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    store_embedding_success,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .embedding_test_support import (
    TEST_EMBEDDING_SPECIFICATION,
    StubEmbeddingModel,
    build_test_manifest,
    unit_vector,
)
from .migrated_db import prepare_test_database

SERVICE = "pantaray_agents.local_runtime.memory_catalog.search_service."


@contextmanager
def _sqlite_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    # Enough of the schema for the service's own queries; the catalog search
    # itself is stubbed out in the tests that use this.
    connection.execute(
        """
        CREATE TABLE memory_embedding_work (
            user_id TEXT, generation_id INTEGER, state TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE memory_embedding_user_generations (
            user_id TEXT, generation_id INTEGER, state TEXT
        )
        """
    )
    try:
        yield connection
    finally:
        connection.close()


def _install_model(
    monkeypatch: pytest.MonkeyPatch, model: StubEmbeddingModel
) -> StubEmbeddingModel:
    monkeypatch.setattr(SERVICE + "load_local_embedding_model", lambda: model)
    return model


def _install_unavailable_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> StubEmbeddingModel:
        raise LocalEmbeddingUnavailableError("model directory is not set")

    monkeypatch.setattr(SERVICE + "load_local_embedding_model", unavailable)


def _epoch_with_item(
    *,
    epoch_id: str,
    handle_fragment: str,
    reference_depth: MemoryReferenceDepth,
) -> tuple[MemoryContextEpoch, str]:
    epoch, item = append_memory_context_item(
        epoch=MemoryContextEpoch(epoch_id, "run-1", "user-1", ()),
        source="fact",
        label="facts",
        source_path="facts/index.md",
        heading_path=None,
        content="shared fact",
        fragment_id=handle_fragment,
        revision_id="revision-1",
        node_id="node-1",
        reference_depth=reference_depth,
    )
    return epoch, item.item.context_handle


def _initial_building_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES (
                    'user-1', 'ja',
                    '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z'
                )
                """
            )
            register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="fact",
                source_record_id="fact-initial-building",
                content="verified authentication recovery procedure",
            )
    ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    return db_path


def _activate_initial_embeddings(db_path: Path) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with immediate_transaction(connection):
            rows = connection.execute(
                """
                SELECT work.generation_id, work.fragment_id, work.chunk_index,
                       fragments.content_sha256
                FROM memory_embedding_work AS work
                JOIN memory_fragments AS fragments
                  ON fragments.user_id = work.user_id
                 AND fragments.fragment_id = work.fragment_id
                ORDER BY work.fragment_id, work.chunk_index
                """
            ).fetchall()
            assert rows
            generation_id = int(rows[0][0])
            for row in rows:
                store_embedding_success(
                    connection,
                    user_id="user-1",
                    generation_id=int(row[0]),
                    fragment_id=str(row[1]),
                    chunk_index=int(row[2]),
                    fragment_content_sha256=str(row[3]),
                    vector=unit_vector(),
                    created_at="2026-08-09T00:00:00Z",
                )
            activate_embedding_generation_in_transaction(
                connection,
                user_id="user-1",
                generation_id=generation_id,
                activated_at="2026-08-09T00:00:00Z",
            )


@pytest.mark.asyncio
async def test_initial_building_search_serves_exact_and_lexical_without_embedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _initial_building_database(tmp_path)
    model = _install_model(monkeypatch, StubEmbeddingModel())

    exact = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="initial-exact",
            query="fact-initial-building",
            focus="stable_knowledge",
            limit=8,
        ),
    )
    lexical = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="initial-lexical",
            query="recovery procedure",
            focus="stable_knowledge",
            limit=8,
        ),
    )
    semantic_only = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="initial-semantic",
            query="ログイン障害の復旧",
            focus="stable_knowledge",
            limit=8,
        ),
    )

    # Nothing is active yet, so there is no index to compare a query vector with.
    assert model.queries == []
    assert [item["content"] for item in exact.results] == [
        "verified authentication recovery procedure"
    ]
    assert exact.results[0]["match_kind"] == "exact"
    assert [item["content"] for item in lexical.results] == [
        "verified authentication recovery procedure"
    ]
    assert lexical.results[0]["match_kind"] == "lexical"
    assert semantic_only.results == ()
    assert semantic_only.semantic_status == "not_ready"


@pytest.mark.asyncio
async def test_missing_model_keeps_exact_and_lexical_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _initial_building_database(tmp_path)
    _activate_initial_embeddings(db_path)
    _install_unavailable_model(monkeypatch)

    exact = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="missing-model-exact",
            query="fact-initial-building",
            focus="stable_knowledge",
            limit=8,
        ),
    )
    lexical = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="missing-model-lexical",
            query="recovery procedure",
            focus="stable_knowledge",
            limit=8,
        ),
    )

    assert exact.results[0]["match_kind"] == "exact"
    assert lexical.results[0]["match_kind"] == "lexical"
    assert exact.semantic_status == lexical.semantic_status == "provider_unavailable"
    assert exact.semantic_error_code == MEMORY_EMBEDDING_MODEL_UNAVAILABLE


@pytest.mark.asyncio
async def test_execute_memory_search_uses_one_generation_in_one_read_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = EmbeddingGeneration(
        generation_id=7,
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    base_epoch, canonical_handle = _epoch_with_item(
        epoch_id="base",
        handle_fragment="shared",
        reference_depth=1,
    )
    search_epoch, searched_handle = _epoch_with_item(
        epoch_id="search",
        handle_fragment="shared",
        reference_depth=0,
    )
    result: MemorySearchResult = {
        "source": "facts",
        "record_id": "fact-1",
        "content": "shared fact",
        "source_path": "facts/index.md",
        "heading_path": None,
        "observed_at": "2026-08-09T00:00:00Z",
        "match_kind": "semantic",
        "context_handle": searched_handle,
    }
    model = _install_model(monkeypatch, StubEmbeddingModel())
    generation_loads = 0
    events: list[str] = []
    pinned_revisions: dict[MemorySource, str | None] = {"fact": "revision-1"}

    def load_generation(connection, *, user_id):
        nonlocal generation_loads
        generation_loads += 1
        events.append(f"load:{connection.in_transaction}")
        assert user_id == "user-1"
        return generation

    def search(connection, **kwargs):
        events.append("search")
        assert connection.in_transaction
        assert kwargs["embedding_generation"] is generation
        assert kwargs["pinned_revisions"] is pinned_revisions
        assert kwargs["query_embedding"] == unit_vector()
        return [result], search_epoch

    monkeypatch.setattr(
        SERVICE + "open_memory_catalog_connection",
        lambda **_kwargs: _sqlite_connection(),
    )
    monkeypatch.setattr(SERVICE + "load_active_embedding_generation", load_generation)
    monkeypatch.setattr(SERVICE + "search_memory_catalog", search)

    response = await execute_memory_search(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="run-1",
            query="shared",
            focus="stable_knowledge",
            limit=8,
            pinned_revisions=pinned_revisions,
            current_epoch=base_epoch,
        ),
    )

    assert generation_loads == 2
    assert model.queries == ["shared"]
    assert events == ["load:False", "load:True", "search"]
    assert response.semantic_status == "available"
    assert response.results[0]["context_handle"] == canonical_handle
    assert response.epoch.items[0].reference_depth == 0


@pytest.mark.asyncio
async def test_index_built_by_another_artifact_is_not_searched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    superseded = EmbeddingGeneration(
        3,
        current_embedding_specification(
            build_test_manifest(artifact_revision="fedcba9876543210")
        ),
    )
    search_kwargs: dict[str, object] = {}

    def search(**kwargs):
        search_kwargs.update(kwargs)
        return [], MemoryContextEpoch("epoch-1", "run-1", "user-1", ())

    model = _install_model(monkeypatch, StubEmbeddingModel())
    monkeypatch.setattr(
        SERVICE + "open_memory_catalog_connection",
        lambda **_kwargs: _sqlite_connection(),
    )
    monkeypatch.setattr(
        SERVICE + "load_active_embedding_generation",
        lambda *_args, **_kwargs: superseded,
    )
    monkeypatch.setattr(SERVICE + "search_memory_catalog", search)

    response = await execute_memory_search(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="run-1",
            query="query",
            focus="all",
            limit=8,
        ),
    )

    assert model.queries == []
    assert search_kwargs["query_embedding"] is None
    assert search_kwargs["embedding_generation"] is None
    assert response.semantic_status == "not_ready"
    assert response.semantic_error_code is None


@pytest.mark.asyncio
async def test_execute_memory_search_reports_active_switch_and_runs_local_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = EmbeddingGeneration(3, TEST_EMBEDDING_SPECIFICATION)
    replacement = EmbeddingGeneration(4, TEST_EMBEDDING_SPECIFICATION)
    generations = iter((current, replacement))
    search_kwargs: dict[str, object] = {}

    def search(**kwargs):
        search_kwargs.update(kwargs)
        return [], MemoryContextEpoch("epoch-1", "run-1", "user-1", ())

    _install_model(monkeypatch, StubEmbeddingModel())
    monkeypatch.setattr(
        SERVICE + "open_memory_catalog_connection",
        lambda **_kwargs: _sqlite_connection(),
    )
    monkeypatch.setattr(
        SERVICE + "load_active_embedding_generation",
        lambda *_args, **_kwargs: next(generations),
    )
    monkeypatch.setattr(SERVICE + "search_memory_catalog", search)

    response = await execute_memory_search(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=1_000,
        request=MemorySearchRequest(
            user_id="user-1",
            run_id="run-1",
            query="query",
            focus="all",
            limit=8,
        ),
    )

    assert search_kwargs["query_embedding"] is None
    assert search_kwargs["embedding_generation"] is None
    assert response.semantic_status == "generation_changed"
    assert response.semantic_error_code is None


@pytest.mark.asyncio
async def test_a_search_leaves_the_event_loop_free_while_it_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _initial_building_database(tmp_path)
    _install_model(monkeypatch, StubEmbeddingModel())
    reading = threading.Event()
    release = threading.Event()
    search = search_service.search_memory_catalog

    def slow_search(**kwargs):  # noqa: ANN003, ANN202
        reading.set()
        # A large catalog reads for seconds; the loop must keep serving others.
        assert release.wait(timeout=5)
        return search(**kwargs)

    monkeypatch.setattr(SERVICE + "search_memory_catalog", slow_search)
    running = asyncio.create_task(
        execute_memory_search(
            db_path=db_path,
            busy_timeout_ms=1_000,
            request=MemorySearchRequest(
                user_id="user-1",
                run_id="off-loop",
                query="fact-initial-building",
                focus="stable_knowledge",
                limit=8,
            ),
        )
    )
    while not reading.is_set():
        await asyncio.sleep(0.01)
    release.set()

    assert (await running).results
