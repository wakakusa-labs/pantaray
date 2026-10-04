from __future__ import annotations

import sqlite3
import tracemalloc
from pathlib import Path

import pytest

import pantaray_agents.agents.action_agent.services.memory_sql as memory_sql_module
from pantaray_agents.agents.action_agent.services.memory_sql import execute_memory_sql
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)

BUSY_TIMEOUT_MS = 1_000


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(
                user_id,
                ui_language,
                created_at,
                updated_at
            ) VALUES ('user-1', 'ja', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO users(
                user_id,
                ui_language,
                created_at,
                updated_at
            ) VALUES ('user-2', 'ja', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id,
                user_id,
                status,
                answer,
                prompt_text,
                response_text,
                prompt_name,
                prompt_version,
                has_suggestion,
                interaction_contract,
                created_at,
                updated_at
            ) VALUES (
                'sug-1',
                'user-1',
                'success',
                'remember the sandbox git finding',
                'prompt',
                'response',
                'prompt',
                'v1',
                1,
                'action_offer',
                '2026-04-01T00:00:00Z',
                '2026-04-01T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id,
                user_id,
                status,
                answer,
                prompt_text,
                response_text,
                prompt_name,
                prompt_version,
                has_suggestion,
                interaction_contract,
                created_at,
                updated_at
            ) VALUES (
                'sug-2',
                'user-2',
                'success',
                'other user memory',
                'prompt',
                'response',
                'prompt',
                'v1',
                1,
                'action_offer',
                '2026-04-01T00:00:00Z',
                '2026-04-01T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO activity_logs(
                log_id,
                user_id,
                period_start,
                period_end,
                status,
                description,
                prompt_name,
                prompt_version,
                created_at,
                updated_at
            ) VALUES (
                'log-1',
                'user-1',
                '2026-04-01T00:00:00Z',
                '2026-04-01T00:05:00Z',
                'success',
                ?,
                'prompt',
                'v1',
                '2026-04-01T00:05:00Z',
                '2026-04-01T00:05:00Z'
            )
            """,
            ("x" * 5_000,),
        )
        _insert_memory_artifact_block(
            connection,
            user_id="user-1",
            artifact_id="artifact-user-1",
            file_id="file-user-1",
            block_id="block-user-1",
            preview_text="current user artifact block",
        )
        _insert_memory_artifact_block(
            connection,
            user_id="user-2",
            artifact_id="artifact-user-2",
            file_id="file-user-2",
            block_id="block-user-2",
            preview_text="other user artifact block",
        )
    return db_path


def _insert_memory_artifact_block(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    artifact_id: str,
    file_id: str,
    block_id: str,
    preview_text: str,
) -> None:
    connection.execute(
        """
        INSERT INTO memory_artifacts(
            artifact_id,
            user_id,
            source_type,
            source_record_id,
            root_path,
            content_sha256,
            logical_created_at,
            logical_updated_at,
            indexed_at
        ) VALUES (?, ?, 'long_term_insight', ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            user_id,
            artifact_id,
            f"artifacts/{artifact_id}",
            f"sha-{artifact_id}",
            "2026-04-01T00:00:00Z",
            "2026-04-01T00:00:00Z",
            "2026-04-01T00:00:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO memory_artifact_files(
            file_id,
            artifact_id,
            relative_path,
            sha256,
            byte_size,
            mime_type,
            updated_at
        ) VALUES (?, ?, 'index.md', ?, 10, 'text/markdown', ?)
        """,
        (file_id, artifact_id, f"sha-{file_id}", "2026-04-01T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO memory_artifact_blocks(
            block_id,
            file_id,
            block_kind,
            heading_path,
            block_index,
            search_text,
            preview_text
        ) VALUES (?, ?, 'paragraph', 'Heading', 0, ?, ?)
        """,
        (block_id, file_id, preview_text, preview_text),
    )


def test_execute_memory_sql_reads_allowed_tables(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(
            "SELECT suggestion_id, answer FROM agent_suggestions "
            "WHERE suggestion_id = 'sug-1'"
        ),
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["columns"] == ["suggestion_id", "answer"]
    assert result.data["rows"] == [
        {
            "suggestion_id": "sug-1",
            "answer": "remember the sandbox git finding",
        }
    ]
    assert result.data["truncated"] is False


def test_execute_memory_sql_scopes_rows_to_current_user(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql="SELECT suggestion_id, answer FROM agent_suggestions ORDER BY suggestion_id",
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == [
        {
            "suggestion_id": "sug-1",
            "answer": "remember the sandbox git finding",
        }
    ]


def test_execute_memory_sql_scopes_source_records_to_current_user(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO activity_logs(log_id, user_id, period_start, period_end,
            status, description, prompt_name, prompt_version, created_at, updated_at)
            VALUES ('log-2', 'user-2', '2026-04-01T00:00:00Z', '2026-04-01T00:05:00Z',
            'success', 'other', 'prompt', 'v1', '2026-04-01T00:05:00Z',
            '2026-04-01T00:05:00Z')"""
        )
        for record_id, user_id, run_id, quote in (
            ("record-1", "user-1", "log-1", "current user quote"),
            ("record-2", "user-2", "log-2", "other user quote"),
        ):
            connection.execute(
                """INSERT INTO source_records(record_id, user_id, run_id, event_id,
                observed_at, source, speaker, shown_time, quote, created_at)
                VALUES (?, ?, ?, 'event-1', '2026-04-01T00:02:00Z', 'Doc', '', '',
                ?, '2026-04-01T00:05:00Z')""",
                (record_id, user_id, run_id, quote),
            )

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(
            "SELECT record_id, quote FROM source_records "
            "WHERE observed_at >= '2026-04-01T00:00:00Z' ORDER BY observed_at"
        ),
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == [
        {"record_id": "record-1", "quote": "current user quote"}
    ]


def test_execute_memory_sql_rejects_main_table_bypass(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql="SELECT suggestion_id FROM main.agent_suggestions",
        limit=20,
    )

    assert result.data is None
    assert result.error is not None


def test_execute_memory_sql_rejects_cte_name_spoof_bypass(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(
            "WITH agent_suggestions AS ("
            "SELECT suggestion_id FROM main.agent_suggestions"
            ") SELECT suggestion_id FROM agent_suggestions"
        ),
        limit=20,
    )

    assert result.data is None
    assert result.error is not None


def test_execute_memory_sql_allows_non_conflicting_cte_names(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(
            "WITH recent AS ("
            "SELECT suggestion_id, answer FROM agent_suggestions"
            ") SELECT suggestion_id, answer FROM recent"
        ),
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == [
        {
            "suggestion_id": "sug-1",
            "answer": "remember the sandbox git finding",
        }
    ]


@pytest.mark.parametrize(
    "sql",
    [
        (
            'WITH "{view}" AS (SELECT suggestion_id FROM "main"."agent_suggestions") '
            'SELECT suggestion_id FROM "{view}" '
            "UNION ALL SELECT suggestion_id FROM agent_suggestions WHERE 0"
        ),
        (
            "WITH '{view}' AS (SELECT suggestion_id FROM 'main'.'agent_suggestions') "
            "SELECT suggestion_id FROM '{view}' "
            "UNION ALL SELECT suggestion_id FROM agent_suggestions WHERE 0"
        ),
        (
            'WITH "{view}" AS (SELECT suggestion_id FROM "main".agent_suggestions) '
            'SELECT suggestion_id, 1 AS agent_suggestions FROM "{view}"'
        ),
    ],
)
@pytest.mark.parametrize(
    "view",
    ["__memory_sql_agent_suggestions", "memory_sql_agent_suggestions"],
)
def test_execute_memory_sql_rejects_cte_named_like_private_view(
    tmp_path: Path,
    sql: str,
    view: str,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=sql.format(view=view),
        limit=20,
    )

    assert result.data is None
    assert result.error is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'agent_suggestions' AS table_name",
        'SELECT "agent_suggestions" AS table_name',
        "SELECT 1 AS value -- agent_suggestions",
        "SELECT 1 AS agent_suggestions",
        (
            "WITH \"agent_suggestions\"(suggestion_id) AS (VALUES ('fake')) "
            "SELECT suggestion_id FROM agent_suggestions"
        ),
    ],
)
def test_execute_memory_sql_rejects_queries_without_memory_table_reads(
    tmp_path: Path,
    sql: str,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=sql,
        limit=20,
    )

    assert result.data is None
    assert result.error is not None


def test_execute_memory_sql_allows_empty_results_after_memory_table_read(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql="SELECT suggestion_id FROM agent_suggestions WHERE answer LIKE '%missing%'",
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == []


def test_execute_memory_sql_rejects_legacy_artifact_projection_tables(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(
            "SELECT block_id, preview_text FROM memory_artifact_blocks "
            "ORDER BY block_id"
        ),
        limit=20,
    )

    assert result.data is None
    assert result.error == (
        "memory_sql: query must reference at least one allowed memory table."
    )


def test_execute_memory_sql_allows_safe_aggregate_functions(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql="SELECT COUNT(*) AS suggestion_count FROM agent_suggestions",
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == [{"suggestion_count": 1}]


@pytest.mark.parametrize(
    ("sql", "expected_rows"),
    [
        (
            "SELECT suggestion_id FROM agent_suggestions WHERE answer LIKE '%sandbox%'",
            [{"suggestion_id": "sug-1"}],
        ),
        (
            "SELECT suggestion_id FROM agent_suggestions WHERE answer GLOB '*sandbox*'",
            [{"suggestion_id": "sug-1"}],
        ),
        (
            "SELECT lower(status) AS status FROM agent_suggestions",
            [{"status": "success"}],
        ),
    ],
)
def test_execute_memory_sql_allows_common_search_functions(
    tmp_path: Path,
    sql: str,
    expected_rows: list[dict[str, object]],
) -> None:
    db_path = _bootstrap_db(tmp_path)
    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=sql,
        limit=20,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["rows"] == expected_rows


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE agent_suggestions SET answer = 'bad'",
        "PRAGMA table_info(agent_suggestions)",
        "SELECT user_id FROM users",
        (
            'SELECT name FROM "sqlite_temp_master" '
            "UNION ALL SELECT suggestion_id FROM agent_suggestions"
        ),
        (
            'SELECT name FROM "pragma_table_list" '
            "UNION ALL SELECT suggestion_id FROM agent_suggestions"
        ),
        "SELECT format('%1000000s', answer) FROM agent_suggestions",
        "SELECT printf('%1000000s', answer) FROM agent_suggestions",
        "SELECT hex(randomblob(8)) FROM agent_suggestions",
        "SELECT load_extension('x') FROM agent_suggestions",
        "SELECT zeroblob(1000000) FROM agent_suggestions",
    ],
)
def test_execute_memory_sql_rejects_unsafe_or_disallowed_queries(
    tmp_path: Path,
    sql: str,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=sql,
        limit=20,
    )

    assert result.data is None
    assert result.error is not None


def test_execute_memory_sql_limits_rows_and_cell_text(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=("SELECT log_id, description FROM activity_logs ORDER BY period_end DESC"),
        limit=1,
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["row_count"] == 1
    assert result.data["truncated"] is True
    description = result.data["rows"][0]["description"]
    assert isinstance(description, str)
    assert len(description) < 5_000
    assert description.endswith("...")


def _insert_bulk_activity_logs(db_path: Path, *, count: int, description: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?)
            INSERT INTO activity_logs(
                log_id, user_id, period_start, period_end, status, description,
                prompt_name, prompt_version, created_at, updated_at
            )
            SELECT
                'bulk-' || i, 'user-1', printf('2026-04-02T%05d', i),
                printf('2026-04-03T%05d', i), 'success', ?, 'prompt', 'v1',
                '2026-04-02T00:05:00Z', '2026-04-02T00:05:00Z'
            FROM n
            """,
            (count, description),
        )


def _nested_hex(expression: str, depth: int) -> str:
    for _ in range(depth):
        expression = f"hex({expression})"
    return expression


def test_execute_memory_sql_rejects_values_inflated_past_the_length_limit(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    def run(depth: int):
        return execute_memory_sql(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            sql=(
                f"SELECT {_nested_hex('description', depth)} AS inflated "
                "FROM activity_logs WHERE log_id = 'log-1'"
            ),
            limit=1,
        )

    # The 5,000-character description doubles per hex(): ~1.3 MB stays allowed,
    # ~5.1 MB would be built in full before any cell truncation.
    allowed = run(8)
    inflated = run(10)

    assert allowed.error is None
    assert inflated.data is None
    assert inflated.error is not None
    assert "too big" in inflated.error


def test_execute_memory_sql_limits_result_columns(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    def run(sql: str):
        return execute_memory_sql(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            sql=sql,
            limit=1,
        )

    all_tables_joined = run(
        "SELECT * FROM activity_logs, activity_summaries, agent_actions, "
        "agent_facts, agent_insights, agent_suggestions, source_records"
    )
    too_many_columns = run(f"SELECT {', '.join(['log_id'] * 201)} FROM activity_logs")

    assert all_tables_joined.error is None
    assert all_tables_joined.data is not None
    assert len(all_tables_joined.data["columns"]) == 136
    assert too_many_columns.data is None
    assert too_many_columns.error is not None
    assert "too many columns" in too_many_columns.error


def test_execute_memory_sql_limits_statement_length(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    prefix = "SELECT log_id FROM activity_logs WHERE log_id = 'log-1' -- "

    def run(sql: str):
        return execute_memory_sql(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            sql=sql,
            limit=1,
        )

    at_limit = run(prefix.ljust(20_000, "x"))
    over_limit = run(prefix.ljust(20_001, "x"))

    assert at_limit.error is None
    assert at_limit.data is not None
    assert at_limit.data["rows"] == [{"log_id": "log-1"}]
    assert over_limit.data is None
    assert over_limit.error is not None
    assert over_limit.error.startswith("memory_sql rejected query:")
    assert "too large" in over_limit.error


def test_execute_memory_sql_holds_one_row_at_a_time(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_bulk_activity_logs(db_path, count=250, description="abcd")

    tracemalloc.start()
    try:
        # Each cell is 1 MiB; holding a full page of 200 such rows needs ~200 MiB.
        result = execute_memory_sql(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            sql=(
                f"SELECT {_nested_hex('description', 18)} AS inflated "
                "FROM activity_logs WHERE log_id LIKE 'bulk-%'"
            ),
            limit=200,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.error is None
    assert result.data is not None
    assert 0 < result.data["row_count"] < 200
    assert result.data["truncated"] is True
    assert peak_bytes < 16 * 1024 * 1024


def test_execute_memory_sql_stops_a_query_past_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_bulk_activity_logs(db_path, count=100, description="x" * 5_000)
    # Each row builds a ~2.5 MB value in a few opcodes and returns one integer,
    # so neither the row, output, nor value limits stop it.
    monkeypatch.setattr(memory_sql_module, "MEMORY_SQL_MAX_SECONDS", 0.05)

    result = execute_memory_sql(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        sql=(f"SELECT sum(length({_nested_hex('description', 9)})) FROM activity_logs"),
        limit=1,
    )

    assert result.data is None
    assert result.error is not None
    assert "ran longer than 0.05 seconds" in result.error
