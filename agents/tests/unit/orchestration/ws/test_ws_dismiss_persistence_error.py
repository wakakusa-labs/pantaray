from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.handler import (
    SuggestionStatusPersistenceError,
    WSOrchestrationHandler,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult


def _build_repo_mock() -> MagicMock:
    repo = MagicMock()
    repo.get_suggestion_state = AsyncMock(
        return_value=RepositoryResult(
            data={
                "suggestion_id": "sug-1",
                "interaction_contract": "action_offer",
                "user_reaction": None,
            }
        )
    )
    repo.append_process_event_and_project_history = AsyncMock(
        return_value=RepositoryResult(data=MagicMock(sequence=1, inserted=True))
    )
    return repo


def _build_handler() -> tuple[WSOrchestrationHandler, MagicMock]:
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
    return handler, websocket


@pytest.mark.asyncio
async def test_handle_dismiss_persistence_failure_returns_dependency_error() -> None:
    handler, websocket = _build_handler()
    handler._is_suggestion_id_accessible = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handler._get_suggestion_repository = AsyncMock(return_value=_build_repo_mock())  # type: ignore[method-assign]
    handler._persist_suggestion_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=SuggestionStatusPersistenceError("db unavailable")
    )

    await handler.handle_dismiss("sug-1", reason=None)

    sent = [call.args[0] for call in websocket.send_json.call_args_list if call.args]
    error_events = [
        payload
        for payload in sent
        if isinstance(payload, dict) and payload.get("event") == "error"
    ]
    assert error_events

    last_error = error_events[-1]
    data = last_error.get("data") or {}
    meta = last_error.get("meta") or {}
    assert isinstance(data, dict)
    assert isinstance(meta, dict)
    assert data.get("error_code") == "WS_DEPENDENCY_UNAVAILABLE"
    assert meta.get("kind") == "suggestion"
    assert meta.get("suggestion_id") == "sug-1"
    assert meta.get("failure_kind") == "persist_dismiss_reaction"

    reaction_committed = [
        payload
        for payload in sent
        if isinstance(payload, dict)
        and payload.get("event") == "suggestion_reaction_committed"
    ]
    assert reaction_committed == []


@pytest.mark.asyncio
async def test_handle_reject_persistence_failure_returns_dependency_error() -> None:
    handler, websocket = _build_handler()
    handler._is_suggestion_id_accessible = AsyncMock(return_value=True)  # type: ignore[method-assign]
    handler._get_suggestion_repository = AsyncMock(return_value=_build_repo_mock())  # type: ignore[method-assign]
    handler._persist_suggestion_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=SuggestionStatusPersistenceError("db unavailable")
    )

    await handler.handle_reject("sug-1")

    sent = [call.args[0] for call in websocket.send_json.call_args_list if call.args]
    error_events = [
        payload
        for payload in sent
        if isinstance(payload, dict) and payload.get("event") == "error"
    ]
    assert error_events

    last_error = error_events[-1]
    data = last_error.get("data") or {}
    meta = last_error.get("meta") or {}
    assert isinstance(data, dict)
    assert isinstance(meta, dict)
    assert data.get("error_code") == "WS_DEPENDENCY_UNAVAILABLE"
    assert meta.get("kind") == "suggestion"
    assert meta.get("suggestion_id") == "sug-1"
    assert meta.get("failure_kind") == "persist_dismiss_reaction"
