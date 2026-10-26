"""Suggestion 中継完了時の公開イベント契約を検証するテスト。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler


@pytest.mark.asyncio
async def test_ws_suggestion_completion_emits_only_process_completed() -> None:
    """Suggestion完了時に reaction 系イベントを送らず process_completed のみ送る。"""
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    session_store = InMemorySessionStore(max_age_seconds=3600)
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=session_store,
        session_id="sess-1",
        user_id="user-1",
    )

    # Suggestion完了処理を直接呼び出す
    await handler._complete_relayed_suggestion(
        "proc-1",
        "sug-1",
        status="success",
        has_suggestion=True,
        interaction_contract="action_offer",
    )

    sent_events = [
        payload.get("event")
        for call in websocket.send_json.call_args_list
        for payload in [call.args[0]]
        if isinstance(payload, dict)
    ]

    assert sent_events == ["process_completed"]
    completed_payload = websocket.send_json.call_args_list[0].args[0]["data"]
    assert completed_payload == {
        "kind": "suggestion",
        "process_id": "proc-1",
        "has_suggestion": True,
        "suggestion_id": "sug-1",
        "interaction_contract": "action_offer",
        "artifacts": [],
        "status": "success",
    }
