"""A live WS session receives every item appended to its owner's chat."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.local_runtime.chat.store import append_chat_item
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import chat_relay
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.schema.chat import SuggestionEventContent, UserMessageContent

USER = "user-1"


@pytest.fixture(autouse=True)
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(chat_relay, "CHAT_RELAY_TICK_SECONDS", 0.01)
    register_logged_out_owner(USER)
    yield
    reset_logged_out_owner()


def _append(message_id: str, text: str) -> None:
    append_chat_item(
        user_id=USER,
        message_id=message_id,
        content=UserMessageContent(
            kind="user_message", text=text, quote_item_id=None, images=(), files=()
        ),
    )


def _chat_events(websocket: MagicMock) -> list[dict[str, object]]:
    return [
        call.args[0]["data"]["item"]
        for call in websocket.send_json.call_args_list
        if call.args[0]["event"] == "chat_item_appended"
    ]


async def _wait_for_events(websocket: MagicMock, count: int) -> None:
    async with asyncio.timeout(5):
        while len(_chat_events(websocket)) < count:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_items_appended_after_the_session_starts_are_relayed_in_order() -> None:
    _append("before", "already on screen")
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id=USER,
    )
    handler.start_chat_relay()
    try:
        _append("m-1", "first")
        append_chat_item(
            user_id=USER,
            message_id="suggestion:s-1",
            content=SuggestionEventContent(
                kind="suggestion_event", suggestion_id="s-1"
            ),
        )
        await _wait_for_events(websocket, 2)
    finally:
        await handler.close()

    relayed = _chat_events(websocket)
    assert [item["content"]["kind"] for item in relayed] == [
        "user_message",
        "suggestion_event",
    ]
    assert relayed[0]["content"]["text"] == "first"
