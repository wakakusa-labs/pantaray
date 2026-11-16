"""Suggestion の live chunk 送信条件を検証する。

性質:
    - suggestion_chunk を送るのは以下を満たすとき *だけ*:
      - DB行の status が success
      - has_suggestion が True
      - answer が空でない
    - 上記を満たさない場合、suggestion_chunk は送られない。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import suggestion_job_timing, suggestion_relay
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.suggestion_relay import LiveSuggestionProcess
from pantaray_agents.repositories.suggestion_runtime_results import (
    AppendProcessEventResult,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expect_chunk"),
    [
        (
            {"status": "success", "has_suggestion": True, "answer": "Do X"},
            True,
        ),
        (
            {"status": "success", "has_suggestion": True, "answer": ""},
            False,
        ),
        (
            {"status": "success", "has_suggestion": False, "answer": "Do X"},
            False,
        ),
        (
            {"status": "timeout", "has_suggestion": True, "answer": "Do X"},
            False,
        ),
        (
            {"status": "error", "has_suggestion": True, "answer": "Do X"},
            False,
        ),
    ],
)
async def test_suggestion_chunk_is_emitted_iff_success_has_suggestion_and_answer(
    monkeypatch: pytest.MonkeyPatch,
    row: dict,
    expect_chunk: bool,
) -> None:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    monkeypatch.setattr(suggestion_relay, "read_suggestion_todo", lambda **_: None)

    monkeypatch.setattr(
        suggestion_job_timing,
        "SUGGESTION_JOB_POLL_INTERVAL_SECONDS",
        0.01,
    )

    class Repo:
        async def get_suggestion(self, *, user_id: str, suggestion_id: str):  # noqa: ANN001
            base = {"user_id": user_id, "suggestion_id": suggestion_id}
            return RepositoryResult(
                data=dict(base, **row),
            )

        async def append_process_event_and_project_history(  # noqa: ANN001
            self,
            *,
            event_id: str,
            suggestion_id: str,
            user_id: str,
            event_name: str,
            payload: dict,
            action_id: str | None,
        ) -> RepositoryResult[AppendProcessEventResult]:
            _ = (event_id, suggestion_id, user_id, event_name, payload, action_id)
            return RepositoryResult(
                data=AppendProcessEventResult(sequence=1, inserted=True)
            )

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
    repo = Repo()
    handler._get_suggestion_repository = AsyncMock(return_value=repo)  # type: ignore[method-assign]
    handler._get_action_state_repository = AsyncMock(return_value=repo)  # type: ignore[method-assign]

    task = asyncio.create_task(
        handler._relay_suggestion_result(
            LiveSuggestionProcess(process_id="p1", suggestion_id="s1")
        )
    )
    await asyncio.wait_for(task, timeout=2.0)

    sent = [c.args[0] for c in websocket.send_json.call_args_list if c.args]
    events = [m.get("event") for m in sent if isinstance(m, dict)]
    assert "process_completed" in events

    if expect_chunk:
        assert "suggestion_chunk" in events
    else:
        assert "suggestion_chunk" not in events

    last_completed = next(
        m
        for m in reversed(sent)
        if isinstance(m, dict) and m.get("event") == "process_completed"
    )
    pdata = last_completed.get("data") or {}
    assert isinstance(pdata, dict)
    assert pdata.get("status") == row.get("status")
    expected_has_suggestion = (
        None if row.get("has_suggestion") is None else bool(row.get("has_suggestion"))
    )
    assert pdata.get("has_suggestion") == expected_has_suggestion
