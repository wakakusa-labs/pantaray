from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.context_search import (
    LEXICAL_CANDIDATE_POOL_MULTIPLIER,
    merge_memory_search_context,
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.embedding_generations import (
    EmbeddingGeneration,
    activate_embedding_generation_in_transaction,
    ensure_user_embedding_generation,
    load_active_embedding_generation,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.lexical_search import (
    MAX_QUERY_TRIGRAM_WINDOWS,
    lexical_query_notes,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemoryReferenceDepth,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    list_revision_fragments,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_index import (
    MemoryEmbeddingIndexUnavailableError,
    encode_embedding,
    store_embedding_success,
)
from pantaray_agents.local_runtime.memory_catalog.semantic_search import (
    search_semantic_fragments,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.memory_embeddings import MEMORY_EMBEDDING_MAX_TEXT_CHARS
from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

from .embedding_test_support import TEST_EMBEDDING_SPECIFICATION
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
EMBEDDING_DIMENSIONS = 512


@pytest.mark.parametrize(
    ("query_embedding", "embedding_generation"),
    (
        (
            None,
            EmbeddingGeneration(
                1,
                TEST_EMBEDDING_SPECIFICATION,
            ),
        ),
        ((1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))), None),
    ),
)
def test_catalog_search_requires_semantic_inputs_together(
    query_embedding: tuple[float, ...] | None,
    embedding_generation: EmbeddingGeneration | None,
) -> None:
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="must be provided together"):
            search_memory_catalog(
                connection=connection,
                user_id="user-1",
                run_id="invalid-semantic-input",
                query="query",
                query_embedding=query_embedding,
                embedding_generation=embedding_generation,
                focus="all",
                center_time=None,
                radius_hours=None,
                limit=8,
            )


def _vector(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


def _raise_operational_error(
    error: sqlite3.OperationalError,
) -> Callable[..., int]:
    def _raise(*_args: object, **_kwargs: object) -> int:
        raise error

    return _raise


def test_semantic_search_preserves_retryable_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.semantic_search._verify_projected_chunk",
        _raise_operational_error(locked),
    )

    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            search_semantic_fragments(
                connection,
                user_id="user-1",
                visible_revision_ids=("revision-1",),
                query_embedding=_vector(0),
                embedding_generation=EmbeddingGeneration(
                    1,
                    TEST_EMBEDDING_SPECIFICATION,
                ),
                candidate_limit=8,
            )

    assert exc_info.value is locked
    assert is_retryable_repository_exception(exc_info.value) is True


def test_semantic_search_maps_missing_schema_to_index_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.memory_catalog.semantic_search._verify_projected_chunk",
        _raise_operational_error(
            sqlite3.OperationalError("no such table: memory_embedding_entries_g1")
        ),
    )

    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(
            MemoryEmbeddingIndexUnavailableError,
            match="schema is unavailable",
        ):
            search_semantic_fragments(
                connection,
                user_id="user-1",
                visible_revision_ids=("revision-1",),
                query_embedding=_vector(0),
                embedding_generation=EmbeddingGeneration(
                    1,
                    TEST_EMBEDDING_SPECIFICATION,
                ),
                candidate_limit=8,
            )


def _scoped_generation_id(connection: sqlite3.Connection) -> int:
    """The user's only generation. Its id depends on what the store held before."""
    row = connection.execute(
        """
        SELECT generation_id FROM memory_embedding_user_generations
        WHERE user_id = 'user-1'
        """
    ).fetchone()
    assert row is not None
    return int(row[0])


def _active_generation(connection: sqlite3.Connection) -> EmbeddingGeneration:
    generation = load_active_embedding_generation(connection, user_id="user-1")
    assert generation is not None
    return generation


def _index_pending_fragments(
    connection: sqlite3.Connection,
    *,
    vector: tuple[float, ...] | None = None,
) -> None:
    rows = connection.execute(
        """
        SELECT fragments.user_id, work.generation_id, work.chunk_index,
               fragments.fragment_id, fragments.content_sha256
        FROM memory_embedding_work AS work
        JOIN memory_fragments AS fragments
          ON fragments.user_id = work.user_id
         AND fragments.fragment_id = work.fragment_id
        WHERE work.state = 'pending'
        ORDER BY fragments.fragment_id
        """
    ).fetchall()
    for row in rows:
        store_embedding_success(
            connection,
            user_id=str(row["user_id"]),
            generation_id=int(row["generation_id"]),
            fragment_id=str(row["fragment_id"]),
            chunk_index=int(row["chunk_index"]),
            fragment_content_sha256=str(row["content_sha256"]),
            vector=vector or _vector(0),
            created_at="2026-07-18T00:00:00Z",
        )
    building = connection.execute(
        """
        SELECT generation_id FROM memory_embedding_user_generations
        WHERE user_id = 'user-1' AND state = 'building'
        """
    ).fetchone()
    if building is not None:
        activate_embedding_generation_in_transaction(
            connection,
            user_id="user-1",
            generation_id=int(building[0]),
            activated_at="2026-07-18T00:00:00Z",
        )


def _append_context_item(
    *,
    epoch: MemoryContextEpoch,
    fragment_id: str,
    reference_depth: MemoryReferenceDepth,
) -> tuple[MemoryContextEpoch, str]:
    merged, item = append_memory_context_item(
        epoch=epoch,
        source="fact",
        label=fragment_id,
        source_path=f"facts/{fragment_id}.md",
        heading_path=None,
        content=fragment_id,
        fragment_id=fragment_id,
        revision_id=f"revision-{fragment_id}",
        node_id=f"node-{fragment_id}",
        reference_depth=reference_depth,
    )
    return merged, item.item.context_handle


def _connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    configure_connection(connection, BUSY_TIMEOUT_MS)
    connection.execute(
        """
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
        VALUES ('user-1', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
        """
    )
    connection.commit()
    ensure_user_embedding_generation(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    return connection


def test_catalog_search_returns_only_active_current_fragments(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-search",
            content="obsolete boundary wording",
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-search",
            content="current boundary wording",
        )
        _index_pending_fragments(connection)
        results, epoch = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-1",
            query="current boundary",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="activity",
            center_time=None,
            radius_hours=None,
            limit=10,
        )
        old_results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-2",
            query="obsolete",
            query_embedding=_vector(1),
            embedding_generation=_active_generation(connection),
            focus="all",
            center_time=None,
            radius_hours=None,
            limit=10,
        )

    assert [row["content"] for row in results] == ["current boundary wording"]
    assert results[0]["context_handle"] == epoch.items[0].item.context_handle
    assert [row["content"] for row in old_results] == ["current boundary wording"]
    assert old_results[0]["match_kind"] == "semantic"


def test_catalog_search_time_hint_ranks_without_filtering(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-time-search",
            content="bounded temporal context",
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="log-time-search-far",
            content="bounded temporal context",
        )
        _index_pending_fragments(connection)
        connection.execute(
            """
            UPDATE memory_nodes
            SET updated_at = '2026-07-18T12:00:00Z'
            WHERE user_id = 'user-1' AND source_record_id = 'log-time-search'
            """
        )
        connection.execute(
            """
            UPDATE memory_nodes
            SET updated_at = '2026-07-17T00:00:00Z'
            WHERE user_id = 'user-1' AND source_record_id = 'log-time-search-far'
            """
        )
        inside, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-inside",
            query="temporal context",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="activity",
            center_time="2026-07-18T13:00:00Z",
            radius_hours=2,
            limit=10,
        )
        outside, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-outside",
            query="temporal context",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="activity",
            center_time="2026-07-19T00:00:00Z",
            radius_hours=2,
            limit=10,
        )

    expected = ["log-time-search", "log-time-search-far"]
    assert [row["record_id"] for row in inside] == expected
    assert [row["record_id"] for row in outside] == expected


def test_catalog_semantic_search_uses_vec0_cosine_ordering(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="far-vector",
            content="first unrelated content",
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="activity_log",
            source_record_id="near-vector",
            content="second unrelated content",
        )
        rows = connection.execute(
            """
            SELECT nodes.source_record_id, work.generation_id, work.chunk_index,
                   fragments.fragment_id,
                   fragments.content_sha256
            FROM memory_embedding_work AS work
            JOIN memory_fragments AS fragments
              ON fragments.user_id = work.user_id
             AND fragments.fragment_id = work.fragment_id
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id
             AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = revisions.user_id
             AND nodes.node_id = revisions.node_id
            ORDER BY nodes.source_record_id
            """
        ).fetchall()
        for row in rows:
            store_embedding_success(
                connection,
                user_id="user-1",
                generation_id=int(row["generation_id"]),
                fragment_id=str(row["fragment_id"]),
                chunk_index=int(row["chunk_index"]),
                fragment_content_sha256=str(row["content_sha256"]),
                vector=(
                    _vector(1)
                    if str(row["source_record_id"]) == "near-vector"
                    else _vector(0)
                ),
                created_at="2026-07-18T00:00:00Z",
            )
        activate_embedding_generation_in_transaction(
            connection,
            user_id="user-1",
            generation_id=_scoped_generation_id(connection),
            activated_at="2026-07-18T00:00:00Z",
        )
        results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="semantic-order",
            query="no lexical overlap",
            query_embedding=_vector(1),
            embedding_generation=_active_generation(connection),
            focus="activity",
            center_time=None,
            radius_hours=None,
            limit=2,
        )

    assert [result["record_id"] for result in results] == [
        "near-vector",
        "far-vector",
    ]
    assert results[0]["match_kind"] == "semantic"


def test_semantic_chunks_return_unique_original_fragments(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    long_content = "x" * (MEMORY_EMBEDDING_MAX_TEXT_CHARS + 1)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="long-fragment",
            content=long_content,
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="short-fragment",
            content="short unrelated fragment",
        )
        work_counts = connection.execute(
            """
            SELECT nodes.source_record_id, COUNT(*)
            FROM memory_embedding_work AS work
            JOIN memory_fragments AS fragments
              ON fragments.user_id = work.user_id
             AND fragments.fragment_id = work.fragment_id
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id
             AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = revisions.user_id
             AND nodes.node_id = revisions.node_id
            GROUP BY nodes.source_record_id
            ORDER BY nodes.source_record_id
            """
        ).fetchall()
        _index_pending_fragments(connection)
        results, epoch = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="chunk-dedup",
            query="意味検索だけで見つける問い合わせ",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=2,
        )

    assert [tuple(row) for row in work_counts] == [
        ("long-fragment", 2),
        ("short-fragment", 1),
    ]
    assert {row["record_id"] for row in results} == {
        "long-fragment",
        "short-fragment",
    }
    long_result = next(row for row in results if row["record_id"] == "long-fragment")
    assert long_result["content"] == long_content
    long_handle = str(long_result["context_handle"])
    long_item = next(
        item for item in epoch.items if item.item.context_handle == long_handle
    )
    assert long_item.item.content == long_content


def test_semantic_search_filters_single_vector_before_knn_limit(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        visible = register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="visible",
            content="visible fact",
        )
        _index_pending_fragments(connection, vector=_vector(1))
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="hidden",
            content="closer hidden fact",
        )
        _index_pending_fragments(connection, vector=_vector(0))

        actual = search_semantic_fragments(
            connection,
            user_id="user-1",
            visible_revision_ids=(visible.revision_id,),
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            candidate_limit=8,
        )

    assert len(actual) == 1
    assert actual[0].distance == pytest.approx(1.0)


def test_semantic_search_batches_all_vectors_before_deduplication_and_limit(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    connection.execute(
        """
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
        VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
        """
    )
    connection.commit()
    ensure_user_embedding_generation(
        db_path=tmp_path / "runtime.db",
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-2",
        specification=TEST_EMBEDDING_SPECIFICATION,
    )
    with immediate_transaction(connection):
        # Closer vectors outside the caller's user/revision scope must stay excluded.
        hidden_revisions = [
            register_inline_domain_memory(
                connection=connection,
                user_id=user_id,
                source="fact",
                source_record_id="hidden",
                content="hidden memory",
            )
            for user_id in ("user-1", "user-2")
        ]
        _index_pending_fragments(connection)
        revision = register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="large-visible-memory",
            content="\n\n".join(
                ["x" * (MEMORY_EMBEDDING_MAX_TEXT_CHARS + 1)]
                + [f"paragraph {index}" for index in range(4_096)]
            ),
        )
        work = connection.execute(
            """
            SELECT work.fragment_id, work.chunk_index,
                   fragments.content_sha256, length(fragments.content_text) AS size
            FROM memory_embedding_work AS work
            JOIN memory_fragments AS fragments
              ON fragments.user_id = work.user_id
             AND fragments.fragment_id = work.fragment_id
            WHERE work.user_id = 'user-1' AND fragments.revision_id = ?
            ORDER BY work.chunk_index, size DESC, work.fragment_id DESC
            """,
            (revision.revision_id,),
        ).fetchall()
        assert len(work) == 4_098
        long_fragment_id = str(work[0]["fragment_id"])
        assert str(work[-1]["fragment_id"]) == long_fragment_id
        for index, row in enumerate(work):
            # The final batch improves the first fragment and contains tied hits.
            vector = (
                (0.6, 0.8, *([0.0] * (EMBEDDING_DIMENSIONS - 2)))
                if index in (1, len(work) - 2, len(work) - 1)
                else _vector(1)
            )
            store_embedding_success(
                connection,
                user_id="user-1",
                generation_id=_scoped_generation_id(connection),
                fragment_id=str(row["fragment_id"]),
                chunk_index=int(row["chunk_index"]),
                fragment_content_sha256=str(row["content_sha256"]),
                vector=vector,
                created_at="2026-07-18T00:00:00Z",
            )
        query_blob = encode_embedding(_vector(0), dimensions=EMBEDDING_DIMENSIONS)
        generation_id = _scoped_generation_id(connection)
        expected = connection.execute(
            f"""
            SELECT entries.fragment_id, entries.fragment_content_sha256,
                   MIN(vec_distance_cosine(vectors.embedding, ?)) AS distance
            FROM memory_embedding_vectors_g{generation_id} AS vectors
            JOIN memory_embedding_entries_g{generation_id} AS entries
              ON entries.embedding_id = vectors.embedding_id
            WHERE entries.user_id = 'user-1' AND entries.revision_id = ?
            GROUP BY entries.fragment_id, entries.fragment_content_sha256
            ORDER BY distance, entries.fragment_id
            """,
            (query_blob, revision.revision_id),
        ).fetchall()
        assert len(expected) == 4_097
        assert long_fragment_id in {str(row[0]) for row in expected[:3]}
        for limit in (24, len(expected)):
            actual = search_semantic_fragments(
                connection,
                user_id="user-1",
                visible_revision_ids=(
                    revision.revision_id,
                    hidden_revisions[1].revision_id,
                ),
                query_embedding=_vector(0),
                embedding_generation=_active_generation(connection),
                candidate_limit=limit,
            )
            assert [(hit.fragment_id, hit.content_sha256) for hit in actual] == [
                (str(row[0]), str(row[1])) for row in expected[:limit]
            ]
            assert [hit.distance for hit in actual] == pytest.approx(
                [float(row[2]) for row in expected[:limit]], abs=1e-6
            )


def test_catalog_search_uses_semantic_backfill_for_japanese_paraphrase(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-authentication",
            content="認証処理が失敗した場合の調査手順",
        )
        _index_pending_fragments(connection, vector=_vector(7))
        results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-semantic-ja",
            query="ログインできない",
            query_embedding=_vector(7),
            embedding_generation=_active_generation(connection),
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert [row["record_id"] for row in results] == ["fact-authentication"]
    assert results[0]["match_kind"] == "semantic"


def _ranked_lexical_record_ids(
    connection: sqlite3.Connection, *, query: str
) -> list[str]:
    results, _ = search_memory_catalog(
        connection=connection,
        user_id="user-1",
        run_id=f"lexical-{query}",
        query=query,
        query_embedding=None,
        embedding_generation=None,
        focus="stable_knowledge",
        center_time=None,
        radius_hours=None,
        limit=8,
    )
    assert [row["match_kind"] for row in results] == ["lexical"] * len(results)
    return [str(row["record_id"]) for row in results]


def _lexical_record_ids(connection: sqlite3.Connection, *, query: str) -> list[str]:
    return sorted(_ranked_lexical_record_ids(connection, query=query))


def _register_facts(
    connection: sqlite3.Connection, facts: tuple[tuple[str, str], ...]
) -> None:
    for record_id, content in facts:
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id=record_id,
            content=content,
        )


def test_lexical_lane_matches_japanese_terms_of_two_or_more_characters(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    connection.execute(
        """
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
        VALUES ('user-2', 'ja', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')
        """
    )
    connection.commit()
    with immediate_transaction(connection):
        for user_id, record_id, content in (
            ("user-1", "fact-expense", "サクラクレパスの経費精算を申請した"),
            (
                "user-1",
                "fact-vendor",
                "Xer のエンタープライズサーチを見積もり事前検証する",
            ),
            ("user-1", "fact-unrelated", "社内イベントの案内"),
            ("user-2", "fact-other-owner", "サクラクレパスの見積を申請した"),
        ):
            register_inline_domain_memory(
                connection=connection,
                user_id=user_id,
                source="fact",
                source_record_id=record_id,
                content=content,
            )
        matched = {
            query: _lexical_record_ids(connection, query=query)
            for query in (
                "申請",
                "見積",
                "経費精算",
                "サクラクレパス",
                "Xer サクラクレパス 見積 事前検証",
            )
        }

    assert matched == {
        "申請": ["fact-expense"],
        "見積": ["fact-vendor"],
        "経費精算": ["fact-expense"],
        "サクラクレパス": ["fact-expense"],
        "Xer サクラクレパス 見積 事前検証": ["fact-expense", "fact-vendor"],
    }


def test_lexical_lane_keeps_short_terms_when_indexed_terms_fill_the_pool(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    limit = 2
    with immediate_transaction(connection):
        for index in range(limit * LEXICAL_CANDIDATE_POOL_MULTIPLIER):
            register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="fact",
                source_record_id=f"fact-indexed-{index}",
                content=f"エンタープライズサーチの検証記録 {index}",
            )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-short-term",
            content="見積の稟議メモ",
        )
        results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="lexical-short-term",
            query="エンタープライズサーチ 見積",
            query_embedding=None,
            embedding_generation=None,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=limit,
        )

    assert "fact-short-term" in [str(row["record_id"]) for row in results]


def test_lexical_lane_matches_short_acronyms_and_numbers_as_whole_words(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        _register_facts(
            connection,
            (
                ("fact-roadmap", "AI ロードマップの設計方針を決めた"),
                ("fact-gmail", "Gmail の下書きを確認した"),
                ("fact-domain", "domain の一覧を整理した"),
                ("fact-training", "training データの取り扱いを見直した"),
                ("fact-pr", "PRを #7 で出した"),
                ("fact-other-issue", "#77 を閉じた"),
                ("fact-longer-issue", "#777 と abc#77suffix を見た"),
                ("fact-api", "API v2 へ移行した"),
            ),
        )
        matched = {
            query: _lexical_record_ids(connection, query=query)
            for query in ("AI 設計方針", "AI", "ai", "PR", "#7", "V2", "#77")
        }

    assert matched == {
        "AI 設計方針": ["fact-roadmap"],
        "AI": ["fact-roadmap"],
        "ai": [],
        "PR": ["fact-pr"],
        "#7": ["fact-pr"],
        "V2": ["fact-api"],
        "#77": ["fact-other-issue"],
    }


def test_lexical_query_notes_name_what_the_word_lanes_skipped() -> None:
    assert lexical_query_notes("PR #7 の設計方針") == ()
    (skipped,) = lexical_query_notes("is a PR open")
    assert skipped.startswith("Not matched by words: is, a.")
    (windows,) = lexical_query_notes("あ" * (MAX_QUERY_TRIGRAM_WINDOWS + 3))
    assert f"first {MAX_QUERY_TRIGRAM_WINDOWS} three-character pieces" in windows


def test_lexical_lane_keeps_ascii_words_whole(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        _register_facts(
            connection,
            (
                ("fact-notion", "Notion の設計メモを整理した"),
                ("fact-motion", "motion blur の調整を検討した"),
                ("fact-notify", "notify の設定を確認した"),
                ("fact-ion", "ion エンジンの実験記録"),
            ),
        )
        matched = _lexical_record_ids(connection, query="notion")

    assert matched == ["fact-notion"]


def test_lexical_lane_matches_a_japanese_sentence_written_without_spaces(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        _register_facts(
            connection,
            (
                (
                    "fact-review",
                    "先週おこなった設計レビューで、保存方針を見直すと決まった",
                ),
                ("fact-schedule", "打ち合わせの時間が決まった"),
                ("fact-expense", "経費精算の申請フローを整理した"),
            ),
        )
        ranked = _ranked_lexical_record_ids(
            connection, query="先週の設計レビューで決まった方針"
        )

    assert ranked == ["fact-review", "fact-schedule"]


def test_lexical_lane_reads_fts_syntax_as_plain_words(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        _register_facts(
            connection,
            (
                ("fact-policy", "設計方針の確定"),
                ("fact-near", "near miss の記録を残した"),
                ("fact-other", "無関係な内容"),
            ),
        )
        matched = {
            query: _lexical_record_ids(connection, query=query)
            for query in (
                '"設計方針"',
                "設計方針*",
                "設計方針 -除外",
                "設計方針 OR 契約",
                '設計方針 "',
                "NEAR(設計方針, 契約)",
            )
        }

    assert matched == {
        '"設計方針"': ["fact-policy"],
        "設計方針*": ["fact-policy"],
        "設計方針 -除外": ["fact-policy"],
        "設計方針 OR 契約": ["fact-policy"],
        '設計方針 "': ["fact-policy"],
        "NEAR(設計方針, 契約)": ["fact-near", "fact-policy"],
    }


def test_catalog_search_keeps_structured_exact_match_first(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="REQ-123",
            content="record selected by its exact identifier",
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-lexical",
            content="REQ-123 appears in lexical content",
        )
        _index_pending_fragments(connection)
        results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-exact",
            query="REQ-123",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert [row["record_id"] for row in results] == ["REQ-123", "fact-lexical"]
    assert [row["match_kind"] for row in results] == ["exact", "corroborated"]


def test_pending_current_revision_keeps_exact_and_lexical_then_gains_semantic(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-auth-runbook",
            content="legacy authentication guidance",
        )
        _index_pending_fragments(connection, vector=_vector(1))
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-auth-runbook",
            content="verified authentication recovery procedure",
        )
        generation = _active_generation(connection)

        exact, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="pending-exact",
            query="fact-auth-runbook",
            query_embedding=_vector(7),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        lexical, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="pending-lexical",
            query="recovery procedure",
            query_embedding=_vector(7),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        semantic_before, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="pending-semantic",
            query="ログイン障害の復旧",
            query_embedding=_vector(7),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

        _index_pending_fragments(connection, vector=_vector(7))
        semantic_after, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="projected-semantic",
            query="ログイン障害の復旧",
            query_embedding=_vector(7),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert [row["content"] for row in exact] == [
        "verified authentication recovery procedure"
    ]
    assert exact[0]["match_kind"] == "exact"
    assert [row["content"] for row in lexical] == [
        "verified authentication recovery procedure"
    ]
    assert lexical[0]["match_kind"] == "lexical"
    assert semantic_before == []
    assert [row["content"] for row in semantic_after] == [
        "verified authentication recovery procedure"
    ]
    assert semantic_after[0]["match_kind"] == "semantic"


def test_exact_fragment_id_is_single_and_record_results_use_source_order(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        revision = register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="ordered-record",
            content="# Heading\n\nFirst paragraph.\n\n- Final item\n",
        )
        _index_pending_fragments(connection)
        fragments = list_revision_fragments(
            connection=connection,
            user_id="user-1",
            revision_id=revision.revision_id,
        )
        paragraph = next(item for item in fragments if item.block_kind == "paragraph")
        generation = _active_generation(connection)
        record_results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="ordered-record-search",
            query="ordered-record",
            query_embedding=_vector(0),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        fragment_results, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="single-fragment-search",
            query=paragraph.fragment_id,
            query_embedding=_vector(0),
            embedding_generation=generation,
            focus="stable_knowledge",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert [row["content"] for row in record_results] == [
        "Heading",
        "First paragraph.",
        "Final item",
    ]
    assert [row["content"] for row in fragment_results] == ["First paragraph."]


def test_semantic_search_rejects_stale_projected_content_hash(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="corrupt-projection",
            content="projected fact",
        )
        _index_pending_fragments(connection)
        generation = _active_generation(connection)
        connection.execute(
            f"""
            UPDATE memory_embedding_entries_g{generation.generation_id}
            SET fragment_content_sha256 = ?
            """,
            ("f" * 64,),
        )

        with pytest.raises(
            MemoryEmbeddingIndexUnavailableError,
            match="stale",
        ):
            search_memory_catalog(
                connection=connection,
                user_id="user-1",
                run_id="corrupt-search",
                query="unrelated",
                query_embedding=_vector(0),
                embedding_generation=generation,
                focus="stable_knowledge",
                center_time=None,
                radius_hours=None,
                limit=8,
            )


def test_semantic_search_rejects_missing_completed_projection(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="missing-projection",
            content="projected fact",
        )
        _index_pending_fragments(connection)
        generation = _active_generation(connection)
        connection.execute(
            f"DELETE FROM memory_embedding_entries_g{generation.generation_id}"
        )

        with pytest.raises(
            MemoryEmbeddingIndexUnavailableError,
            match="missing or duplicate",
        ):
            search_memory_catalog(
                connection=connection,
                user_id="user-1",
                run_id="missing-projection-search",
                query="unrelated",
                query_embedding=_vector(0),
                embedding_generation=generation,
                focus="stable_knowledge",
                center_time=None,
                radius_hours=None,
                limit=8,
            )


def test_semantic_search_rejects_a_projected_entry_without_its_vector(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="vectorless-projection",
            content="projected fact",
        )
        _index_pending_fragments(connection)
        generation = _active_generation(connection)
        connection.execute(
            f"DELETE FROM memory_embedding_vectors_g{generation.generation_id}"
        )

        with pytest.raises(
            MemoryEmbeddingIndexUnavailableError,
            match="missing or stale projected vectors",
        ):
            search_memory_catalog(
                connection=connection,
                user_id="user-1",
                run_id="vectorless-search",
                query="unrelated",
                query_embedding=_vector(0),
                embedding_generation=generation,
                focus="stable_knowledge",
                center_time=None,
                radius_hours=None,
                limit=8,
            )


def test_semantic_search_accepts_an_unembeddable_chunk(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        activate_embedding_generation_in_transaction(
            connection,
            user_id="user-1",
            generation_id=_scoped_generation_id(connection),
            activated_at="2026-07-18T00:00:00Z",
        )
        revision = register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="failed-projection",
            content="unavailable semantic fact",
        )
        connection.execute(
            """
            UPDATE memory_embedding_work
            SET state = 'failed', last_error_code = ?
            WHERE user_id = 'user-1'
            """,
            ("MEMORY_EMBEDDING_CONTENT_EMPTY",),
        )

        # A chunk the model cannot embed is left out of the index rather than
        # taking semantic search down with it.
        results = search_semantic_fragments(
            connection,
            user_id="user-1",
            visible_revision_ids=(revision.revision_id,),
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            candidate_limit=8,
        )

    assert results == ()


def test_catalog_search_supports_all_declared_focus_values(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    with immediate_transaction(connection):
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="fact",
            source_record_id="fact-focus",
            content="focus marker",
        )
        register_inline_domain_memory(
            connection=connection,
            user_id="user-1",
            source="action",
            source_record_id="action-focus",
            content="focus marker",
        )
        _index_pending_fragments(connection)
        user_context, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-user-context",
            query="focus marker",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="user_context",
            center_time=None,
            radius_hours=None,
            limit=8,
        )
        agent_work, _ = search_memory_catalog(
            connection=connection,
            user_id="user-1",
            run_id="search-agent-work",
            query="focus marker",
            query_embedding=_vector(0),
            embedding_generation=_active_generation(connection),
            focus="agent_work",
            center_time=None,
            radius_hours=None,
            limit=8,
        )

    assert [row["record_id"] for row in user_context] == ["fact-focus"]
    assert {row["record_id"] for row in agent_work} == {"action-focus"}


def test_merge_memory_search_context_preserves_canonical_handles_and_depth() -> None:
    base = MemoryContextEpoch("epoch-base", "run-1", "user-1", ())
    base, canonical_handle = _append_context_item(
        epoch=base,
        fragment_id="shared",
        reference_depth=1,
    )
    searched = MemoryContextEpoch("epoch-search", "run-1", "user-1", ())
    searched, searched_handle = _append_context_item(
        epoch=searched,
        fragment_id="shared",
        reference_depth=0,
    )
    searched, new_handle = _append_context_item(
        epoch=searched,
        fragment_id="new",
        reference_depth=0,
    )
    results = [
        {"record_id": "shared", "context_handle": searched_handle},
        {"record_id": "new", "context_handle": new_handle},
    ]

    canonical_results, merged = merge_memory_search_context(
        results=results,
        current_epoch=base,
        search_epoch=searched,
    )

    assert canonical_results[0]["context_handle"] == canonical_handle
    assert canonical_results[1]["context_handle"] == new_handle
    assert results[0]["context_handle"] == searched_handle
    assert [item.fragment_id for item in merged.items] == ["shared", "new"]
    assert merged.items[0].item.context_handle == canonical_handle
    assert merged.items[0].reference_depth == 0
