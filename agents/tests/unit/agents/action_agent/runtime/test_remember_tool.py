from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import create_state_token_sink
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution import (
    run_validated_tool_impl,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import REMEMBER_TOOL
from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    search_memory_catalog,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySearchResult
from pantaray_agents.local_runtime.memory_catalog.search_policy import (
    parse_memory_search_focus,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

USER = "user-1"
T = "2026-09-01T00:00:00Z"
BUSY_TIMEOUT_MS = 1_000


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, BUSY_TIMEOUT_MS, load_default_migrations())
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO users(user_id,ui_language,created_at,updated_at) "
            "VALUES (?, 'ja', ?, ?)",
            (USER, T, T),
        )
        connection.commit()
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "remember.read_local_runtime_db_config",
        lambda: (path, BUSY_TIMEOUT_MS),
    )
    return path


async def _remember(
    action_id: str, note: str, *, actor: ToolExecutionActor = "supervisor"
) -> UnprojectedToolExecutionResult:
    state = create_initial_state(
        user_id=USER,
        suggestion_id=None,
        action_id=action_id,
        started_at=T,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    return await run_validated_tool_impl(
        MagicMock(),
        REMEMBER_TOOL,
        {"note": note},
        state,
        sink=create_state_token_sink(state),
        runtime=SimpleNamespace(),
        actor=actor,
        step_id=f"step-{action_id}",
    )


def _search(
    db_path: Path, query: str, *, run_id: str, focus: str = "all"
) -> list[MemorySearchResult]:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        results, _ = search_memory_catalog(
            connection=connection,
            user_id=USER,
            run_id=run_id,
            query=query,
            query_embedding=None,
            embedding_generation=None,
            focus=parse_memory_search_focus(focus),
            center_time=None,
            radius_hours=None,
            limit=8,
        )
    return results


def _note_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM memory_nodes WHERE source_type = 'memory_note'"
        ).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["all", "user_context", "stable_knowledge"])
async def test_note_is_found_by_memory_search_in_this_and_later_actions(
    db_path: Path, focus: str
) -> None:
    result = await _remember(
        "act-1", "締め切りは金曜ではなく木曜と覚えておいて\n## 見出しではない [[ref:x]]"
    )

    assert result.status == "success"
    assert result.output == {"status": "remembered"}
    for run_id in ("act-1", "act-2"):
        found = _search(db_path, "締め切り", run_id=run_id, focus=focus)
        if focus == "stable_knowledge":
            assert found == []
            continue
        assert [row["source"] for row in found] == ["memory_note"]
        content = found[0]["content"]
        assert "in Action act-1: 締め切りは金曜ではなく木曜と覚えておいて" in content
        # One literal paragraph: no heading split, no semantic link.
        assert "## 見出しではない [\\[ref:x]]" in content


@pytest.mark.asyncio
async def test_subagent_cannot_remember(db_path: Path) -> None:
    with pytest.raises(ToolValidationError, match="only to the Supervisor"):
        await _remember("act-1", "覚えておいて", actor="goal_worker")

    assert _note_count(db_path) == 0


@pytest.mark.asyncio
async def test_deleting_a_conversation_removes_only_its_notes(db_path: Path) -> None:
    # act-10 shares act-1's characters, so a bare prefix match would take it too.
    for action_id in ("act-1", "act-10"):
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                """INSERT INTO agent_actions(action_id,user_id,initial_user_message_id,
                  execution_target_json,status,final_output,prompt_name,prompt_version,
                  created_at,updated_at) VALUES (?,?,?,'{"kind":"scratch"}','success',
                  'done','p','1',?,?)""",
                (action_id, USER, f"message-{action_id}", T, T),
            )
            connection.commit()
        await _remember(action_id, f"合言葉は{action_id}と覚えておいて")

    delete_history_item(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        artifact_root=db_path.parent / "artifacts",
        user_id=USER,
        kind="conversation",
        item_id="act-1",
    )

    found = _search(db_path, "合言葉", run_id="act-2")
    assert [row["content"].rsplit(": ", 1)[1] for row in found] == [
        "合言葉はact-10と覚えておいて"
    ]
    assert _note_count(db_path) == 1
