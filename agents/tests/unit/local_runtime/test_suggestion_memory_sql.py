from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.services.memory_sql import (
    MAX_MEMORY_SQL_LIMIT,
    MAX_MEMORY_SQL_OUTPUT_CHARS,
)
from pantaray_agents.agents.artifact_react import ReactToolRegistry
from pantaray_agents.agents.suggestion_agent.react import SUGGESTION_MAX_INPUT_BYTES
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    LocalSuggestionResearchTools,
)

from .test_suggestion_research_tools import (
    BUSY_TIMEOUT_MS,
    TIMESTAMP,
    _bootstrap_db,
    _snapshot,
    _tool_call,
)


def _insert_activity_log(
    connection: sqlite3.Connection, *, log_id: str, user_id: str, description: str
) -> None:
    connection.execute(
        """INSERT INTO activity_logs(log_id, user_id, period_start, period_end,
        status, description, prompt_name, prompt_version, created_at, updated_at)
        VALUES (?, ?, '2026-09-01T00:00:00Z', '2026-09-01T00:15:00Z', 'success', ?,
        'prompt', 'v1', '2026-09-01T00:15:00Z', '2026-09-01T00:15:00Z')""",
        (log_id, user_id, description),
    )


def _registry(db_path: Path) -> ReactToolRegistry:
    runtime = LocalSuggestionResearchTools(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        snapshot=_snapshot(db_path=db_path),
        activity_start=None,
    )
    return ReactToolRegistry(
        runtime.build_tool_definitions(user_id="user-1", run_id="suggestion-1")
    )


@pytest.mark.asyncio
async def test_suggestion_memory_sql_reads_only_the_run_users_rows(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) "
            "VALUES ('user-2', 'ja', ?, ?)",
            (TIMESTAMP, TIMESTAMP),
        )
        _insert_activity_log(
            connection, log_id="log-1", user_id="user-1", description="mine"
        )
        _insert_activity_log(
            connection, log_id="log-2", user_id="user-2", description="theirs"
        )

    result = await _registry(db_path).execute(
        _tool_call(
            "memory_sql",
            {"sql": "SELECT description FROM activity_logs ORDER BY period_start"},
        ),
        1,
    )

    assert result.status == "success"
    assert isinstance(result.output, dict)
    assert result.output["rows"] == [{"description": "mine"}]


@pytest.mark.asyncio
async def test_suggestion_memory_sql_rejects_a_write_as_a_tool_error(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    result = await _registry(db_path).execute(
        _tool_call("memory_sql", {"sql": "DELETE FROM activity_logs"}),
        1,
    )

    assert result.status == "error"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_the_largest_memory_sql_result_fits_half_the_suggestion_input_limit() -> None:
    """Trimming compacts the resent conversation to half the limit and always keeps
    the newest result, so one result above that half would defeat the bound."""
    cell = "あ" * (MAX_MEMORY_SQL_OUTPUT_CHARS // MAX_MEMORY_SQL_LIMIT)
    payload = {
        "columns": ["description"],
        "rows": [{"description": cell} for _ in range(MAX_MEMORY_SQL_LIMIT)],
        "row_count": MAX_MEMORY_SQL_LIMIT,
        "truncated": True,
        "notes": ["memory_sql output was truncated by total character limit."],
    }

    size = len(json.dumps(payload, ensure_ascii=False).encode())

    assert size < SUGGESTION_MAX_INPUT_BYTES // 2
