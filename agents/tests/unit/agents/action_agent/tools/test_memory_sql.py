from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import base_state

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    _run_validated_tool_impl,
    _validate_tool_args,
)
from pantaray_agents.agents.action_agent.services.memory_sql import (
    MEMORY_SQL_ALLOWED_TABLES,
)
from pantaray_agents.agents.action_agent.tools import (
    MEMORY_SQL_TOOL,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_REGISTRY,
)
from pantaray_agents.agents.core import CountingSink
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
            ) VALUES ('user-123', 'ja', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO agent_facts(
                fact_id,
                user_id,
                status,
                facts_profile_brief,
                structured_fact_sha256,
                prompt_name,
                prompt_version,
                created_at,
                updated_at
            ) VALUES (
                'fact-1',
                'user-123',
                'success',
                'docker sandbox fact',
                'sha-fact-1',
                'prompt',
                'v1',
                '2026-04-01T00:00:00Z',
                '2026-04-01T00:00:00Z'
            )
            """
        )
    return db_path


async def _run_tool(
    action_agent: ActionAgent,
    *,
    args: dict[str, object],
    state: dict,
):
    validated_args = dict(args)
    _validate_tool_args(MEMORY_SQL_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        action_agent,
        MEMORY_SQL_TOOL,
        validated_args,  # type: ignore[arg-type]
        state,
        sink=CountingSink(),
        runtime=runtime,
        step_id="step-memory-sql",
    )


def test_memory_sql_tool_is_registered() -> None:
    assert TOOL_REGISTRY["memory_sql"] is MEMORY_SQL_TOOL
    assert "memory_sql" in SUPERVISOR_SINGLE_REACT_TOOL_IDS


def test_memory_sql_model_visible_description_names_every_readable_table() -> None:
    # The model sees only the guide-derived description, so the table guide
    # must live there or the model cannot write a query at all.
    description = MEMORY_SQL_TOOL.prompt_contract.description
    for table in MEMORY_SQL_ALLOWED_TABLES:
        assert f"- {table}:" in description


@pytest.mark.asyncio
async def test_memory_sql_tool_executes_read_only_query(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))

    state = base_state(action_agent)

    result = await _run_tool(
        action_agent,
        args={
            "sql": "SELECT fact_id, facts_profile_brief FROM agent_facts WHERE fact_id = 'fact-1'",
            "limit": 10,
        },
        state=state,
    )

    assert result.status == "success"
    assert result.output["columns"] == ["fact_id", "facts_profile_brief"]
    assert result.output["rows"] == [
        {"fact_id": "fact-1", "facts_profile_brief": "docker sandbox fact"}
    ]
