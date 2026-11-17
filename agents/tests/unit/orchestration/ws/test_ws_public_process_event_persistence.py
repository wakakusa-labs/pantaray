"""公開 process event の durable persistence が fail-closed であることを検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws.base import (
    BaseWSHandler,
    PublicProcessEventPersistenceError,
)
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.schema.websocket.server_messages import (
    ErrorMessage,
    ProcessPausedMessage,
)


class _FakeRepo:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def append_process_event_and_project_history(  # type: ignore[no-untyped-def]
        self, **kwargs
    ) -> RepositoryResult[object]:
        self.calls.append(dict(kwargs))
        return RepositoryResult(
            data=SimpleNamespace(sequence=1, inserted=True),
        )


@pytest.mark.asyncio
async def test_public_error_missing_required_meta_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)

    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    handler = BaseWSHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id="user-1",
    )
    repo = _FakeRepo()
    handler._get_suggestion_repository = AsyncMock(return_value=repo)  # type: ignore[attr-defined]

    with pytest.raises(PublicProcessEventPersistenceError):
        await handler.send_error(
            ErrorMessage(
                error_type="repository_error",
                error_code="DB_DOWN",
                error_message="db down",
                severity="error",
            ),
            process_id="process-1",
            meta={
                "suggestion_id": "suggestion-1",
                "kind": "action",
                "error_code": "DB_DOWN",
                # stage を意図的に欠落させる
            },
        )

    assert repo.calls == []
    websocket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_public_suggestion_error_missing_suggestion_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)

    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    handler = BaseWSHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id="user-1",
    )
    repo = _FakeRepo()
    handler._get_suggestion_repository = AsyncMock(return_value=repo)  # type: ignore[attr-defined]

    with pytest.raises(PublicProcessEventPersistenceError):
        await handler.send_error(
            ErrorMessage(
                error_type="internal_error",
                error_code="SUGGESTION_START_FAILED",
                error_message="failed",
                severity="error",
            ),
            process_id="process-1",
            meta={
                "kind": "suggestion",
                "stage": "start_failed",
                "error_code": "SUGGESTION_START_FAILED",
            },
        )

    assert repo.calls == []
    websocket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_public_session_error_skips_persistence_and_sends_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)

    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    handler = BaseWSHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id="user-1",
    )
    repo = _FakeRepo()
    handler._get_suggestion_repository = AsyncMock(return_value=repo)  # type: ignore[attr-defined]

    await handler.send_error(
        ErrorMessage(
            error_type="validation_error",
            error_code="WS_INVALID_JSON",
            error_message="invalid",
            severity="error",
        ),
        meta={
            "kind": "protocol",
            "stage": "invalid_json",
            "error_code": "WS_INVALID_JSON",
        },
    )

    assert repo.calls == []
    websocket.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_paused_persists_public_event_before_ws_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)

    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    handler = BaseWSHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id="user-1",
    )
    repo = _FakeRepo()
    handler._get_action_state_repository = AsyncMock(return_value=repo)  # type: ignore[attr-defined]

    await handler._send(
        OutboundEvent.PROCESS_PAUSED.value,
        ProcessPausedMessage(
            kind="action",
            process_id="process-1",
            suggestion_id="suggestion-1",
            action_id="action-1",
            command_id="command-1",
            status="processing",
            reason="approval_pending",
            completed_at="2026-04-27T01:18:45Z",
            approval_blockers=[
                {
                    "process_id": "process-1",
                    "action_id": "action-1",
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                    "tool_id": "bash",
                    "intent_class": "process_exec_local",
                    "command_summary": {"kind": "bash", "command": "pwd"},
                }
            ],
        ),
        process_id="process-1",
        meta={
            "kind": "action",
            "process_id": "process-1",
            "suggestion_id": "suggestion-1",
            "action_id": "action-1",
            "command_id": "command-1",
        },
        event_id="event-paused",
        store_in_session_store=False,
        persist_public_event=True,
    )

    assert repo.calls == [
        {
            "event_id": "event-paused",
            "suggestion_id": "suggestion-1",
            "user_id": "user-1",
            "event_name": OutboundEvent.PROCESS_PAUSED.value,
            "payload": {
                "data": {
                    "kind": "action",
                    "process_id": "process-1",
                    "suggestion_id": "suggestion-1",
                    "action_id": "action-1",
                    "command_id": "command-1",
                    "status": "processing",
                    "reason": "approval_pending",
                    "completed_at": "2026-04-27T01:18:45Z",
                    "approval_blockers": [
                        {
                            "process_id": "process-1",
                            "action_id": "action-1",
                            "approval_session_id": "approval-1",
                            "tool_request_id": "tool-request-1",
                            "tool_id": "bash",
                            "intent_class": "process_exec_local",
                            "command_summary": {"kind": "bash", "command": "pwd"},
                            "review_reason": None,
                        }
                    ],
                },
                "meta": {
                    "kind": "action",
                    "process_id": "process-1",
                    "suggestion_id": "suggestion-1",
                    "action_id": "action-1",
                    "command_id": "command-1",
                },
            },
            "action_id": "action-1",
        }
    ]
    websocket.send_json.assert_awaited_once()
    assert websocket.send_json.await_args.args[0]["sequence"] == 1
