"""A desktop request reaches the user's live session, once, from any Action.

The asking tool runs on a worker thread with its own event loop, as an Action
job does in the app, and the session is served on the test's loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    announce_screen_capture_request,
)
from pantaray_agents.orchestration.ws import action_relay_forwarder_local

from .test_ws_suggestion_relay import _bare_handler, _migrated_db

PROCESS_ID = "process:action-1"


def _capture_requests(websocket: MagicMock) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in websocket.send_json.call_args_list
        if call.args[0].get("event") == "screen_capture_requested"
    ]


def _announce_from_an_action_worker() -> None:
    asyncio.run(
        announce_screen_capture_request(
            user_id="user-1",
            action_id="action-1",
            process_id=PROCESS_ID,
            tool_request_id="action-1:step-1:1",
            capture_request_id="capture-1",
            app_name="Finder",
        )
    )


async def test_a_request_from_an_action_no_session_follows_reaches_the_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat-launched Action is never attached, and its capture must still go out."""

    handler, websocket = _bare_handler(
        monkeypatch, session_id="sess-1", db_path=_migrated_db(tmp_path)
    )
    handler.start_suggestion_relay()
    try:
        await asyncio.to_thread(_announce_from_an_action_worker)
    finally:
        await handler.close()

    [envelope] = _capture_requests(websocket)
    # A control message only: no persisted sequence for a client to replay.
    assert set(envelope) == {"event_id", "event", "data"}
    assert envelope["data"] == {
        "kind": "action",
        "process_id": PROCESS_ID,
        "action_id": "action-1",
        "tool_request_id": "action-1:step-1:1",
        "capture_request_id": "capture-1",
        "app_name": "Finder",
    }


async def test_a_request_from_an_attached_action_is_sent_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the task attaches the Action; its forwarder must not send it again."""

    db_path = _migrated_db(tmp_path)
    monkeypatch.setattr(
        action_relay_forwarder_local,
        "read_local_runtime_db_config",
        lambda: (db_path, 1000),
    )
    handler, websocket = _bare_handler(
        monkeypatch, session_id="sess-1", db_path=db_path
    )
    handler.start_suggestion_relay()
    handler._bind_action_process(PROCESS_ID, None, "action-1")
    handler._start_action_event_forwarder(
        process_id=PROCESS_ID,
        logical_run_id="run-1",
        suggestion_id=None,
        action_id="action-1",
        command_id="command-1",
        start_event_id="event-0",
    )
    try:
        await asyncio.to_thread(_announce_from_an_action_worker)
        # Several forwarder polls (100 ms each) pass before anything is counted.
        await asyncio.sleep(0.5)
    finally:
        await handler.close()

    assert len(_capture_requests(websocket)) == 1
