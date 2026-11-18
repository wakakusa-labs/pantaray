"""The TODO a shown Suggestion became reaches its popup: relayed when it arrives,
and read again, as it stands now, when the popup is opened later."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import apply_migrations
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.todos.clock import read_todo_clock
from pantaray_agents.local_runtime.todos.writes import (
    set_todo_check,
    suggestion_todo_id,
)
from pantaray_agents.orchestration.overlay.bootstrap import OverlayBootstrapService
from pantaray_agents.orchestration.overlay.suggestion_todo import read_suggestion_todo
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import suggestion_job_timing
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.suggestion_relay import LiveSuggestionProcess
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentResponse,
    SuggestionTodo,
    SuggestionTodoOutcome,
)
from pantaray_agents.schema.overlay_bootstrap import SuggestionTodoView

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "sug-1"
TIMESTAMP = "2026-11-16T00:00:00Z"


async def _save_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: SuggestionTodoOutcome
) -> LocalSuggestionRepository:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, BUSY_TIMEOUT_MS, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'ja', ?, ?)",
            (USER_ID, TIMESTAMP, TIMESTAMP),
        )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=MagicMock(),
    )
    await repository.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=TIMESTAMP
    )
    saved = await repository.save_suggestion(
        SuggestionAgentResponse(
            suggestion_id=SUGGESTION_ID,
            user_id=USER_ID,
            created_at=TIMESTAMP,
            answer="The deck is drafted. Shall I add the charts?",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            todo=SuggestionTodo(
                text="Draft the deck",
                outcome=outcome,
                note=None if outcome == "open" else "Charts are left",
            ),
            interaction_contract="action_offer",
        ),
        prompt_name="suggestion",
        prompt_version="test",
    )
    assert saved.error is None
    return repository


@pytest.mark.parametrize(
    ("outcome", "state"),
    [("open", "handed_over"), ("partial", "partial"), ("continue", "waiting")],
)
async def test_the_popup_names_the_todo_as_the_list_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: SuggestionTodoOutcome,
    state: str,
) -> None:
    await _save_offer(tmp_path, monkeypatch, outcome)

    todo = read_suggestion_todo(user_id=USER_ID, suggestion_id=SUGGESTION_ID)

    assert todo is not None
    assert (todo.text, todo.state) == ("Draft the deck", state)


async def test_relayed_on_arrival_and_read_as_it_stands_when_opened_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = await _save_offer(tmp_path, monkeypatch, "continue")
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    monkeypatch.setattr(
        suggestion_job_timing, "SUGGESTION_JOB_POLL_INTERVAL_SECONDS", 0.01
    )
    websocket = MagicMock(client_state=WebSocketState.CONNECTED, send_json=AsyncMock())
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id=USER_ID,
    )
    handler._get_suggestion_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    handler._get_action_state_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    await handler._relay_suggestion_result(
        LiveSuggestionProcess(process_id="process-1", suggestion_id=SUGGESTION_ID)
    )
    sent = [call.args[0] for call in websocket.send_json.call_args_list]
    assert [m["data"]["todo"] for m in sent if m["event"] == "process_completed"] == [
        {"text": "Draft the deck", "state": "waiting"}
    ]

    # The user finishes it in the TODO list; the popup opened afterwards says so.
    register_logged_out_owner(USER_ID)
    try:
        set_todo_check(
            user_id=USER_ID,
            todo_id=suggestion_todo_id(SUGGESTION_ID),
            by="user",
            checked=True,
            clock=read_todo_clock(),
        )
    finally:
        reset_logged_out_owner()
    bootstrap = await OverlayBootstrapService(
        repository, db_path=tmp_path / "runtime.db"
    ).build_for_suggestion(user_id=USER_ID, suggestion_id=SUGGESTION_ID)
    assert bootstrap.snapshot.todo == SuggestionTodoView(
        text="Draft the deck", state="done"
    )
