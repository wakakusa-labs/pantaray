"""A live WS session hears, by revision only, when its owner's TODOs change."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState
from tests.unit.local_runtime.migrated_db import prepare_test_database
from tests.unit.local_runtime.todos.store_support import NOW, USER, T, execute

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.model import Todo
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    update_todo,
)
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import todo_relay
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler


@pytest.fixture(autouse=True)
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(todo_relay, "TODO_RELAY_TICK_SECONDS", 0.01)
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    execute(
        db_path,
        f"INSERT INTO users(user_id, ui_language, created_at, updated_at) "
        f"VALUES ('{USER}', 'ja', '{T}', '{T}');",
    )
    register_logged_out_owner(USER)
    yield
    reset_logged_out_owner()


def _create(client_todo_id: str) -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=client_todo_id,
        project_id=None,
        text="Send the quote",
        week_of="2026-11-04",
        due=None,
        clock=NOW,
    )
    assert isinstance(created, Todo)
    return created


def _handler() -> tuple[WSOrchestrationHandler, MagicMock]:
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id=USER,
    )
    return handler, websocket


def _changes(websocket: MagicMock) -> list[object]:
    return [
        call.args[0]["data"]
        for call in websocket.send_json.call_args_list
        if call.args[0]["event"] == "todo_changed"
    ]


async def _ticks(count: int) -> None:
    await asyncio.sleep(todo_relay.TODO_RELAY_TICK_SECONDS * count)


async def _wait_for_changes(websocket: MagicMock, count: int) -> None:
    async with asyncio.timeout(5):
        while len(_changes(websocket)) < count:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_each_change_is_sent_as_a_revision_and_quiet_ticks_send_nothing() -> None:
    todo = _create("before")  # already in what the client reads
    handler, websocket = _handler()
    handler.start_todo_relay()
    try:
        await _ticks(5)
        assert _changes(websocket) == []
        _create("after")
        await _wait_for_changes(websocket, 1)
        # A replayed delete changes nothing, so it is not news.
        for _ in range(2):
            update_todo(
                user_id=USER,
                todo_id=todo.todo_id,
                step=lambda current: core.delete_by_user(current, now=NOW.now),
            )
        await _wait_for_changes(websocket, 2)
        await _ticks(5)
    finally:
        await handler.close()

    assert _changes(websocket) == [{"revision": 2}, {"revision": 3}]
