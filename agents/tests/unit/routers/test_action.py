from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.local_runtime.action_conversation import cursor_codec as cursor
from pantaray_agents.local_runtime.action_conversation import repository
from pantaray_agents.local_runtime.runtime import runtime_env
from pantaray_agents.local_runtime.storage import migrations
from pantaray_agents.local_runtime.storage import sqlite_vector as vector
from pantaray_agents.routers import action as action_router
from pantaray_agents.schema import action_conversation as conversation

_PAGE = conversation.ActionConversationPage(
    action=conversation.ActionConversationSummary(
        action_id="action-1",
        suggestion_id=None,
        approved_suggestion=None,
        status="success",
        latest_run_id=None,
        resumable=False,
    ),
    runs=(),
    unadopted_messages=(),
    next_cursor="next-page",
)
_STATE_PATH = "/v1/agents/users/user-1/actions/action-1/state"
_READER = "read_action_conversation_page_in_connection"


def _client(resolved_user_id: str = "user-1") -> TestClient:
    app = FastAPI()
    app.include_router(action_router.router)
    app.dependency_overrides[action_router.get_current_user_id_from_token] = lambda: (
        resolved_user_id
    )
    return TestClient(app)


def test_action_state_reads_one_configured_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[object, object]] = []

    def read_page(**kwargs: object) -> conversation.ActionConversationPage:
        connection = kwargs["connection"]
        assert isinstance(connection, sqlite3.Connection)
        assert connection.in_transaction
        assert connection.row_factory is sqlite3.Row
        row = connection.execute(
            "SELECT pantaray_visible_action_tool_id(?)", ("tool::bash",)
        ).fetchone()
        assert row[0] == "bash"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1_500
        assert (kwargs["user_id"], kwargs["action_id"]) == ("user-1", "action-1")
        requests.append((kwargs["cursor"], kwargs["limit"]))
        return _PAGE

    monkeypatch.setenv("LOCAL_DB_PATH", ":memory:")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1500")
    monkeypatch.setattr(
        action_router, "read_action_conversation_page_in_connection", read_page
    )

    client = _client()
    response = client.get(_STATE_PATH)
    client.get(_STATE_PATH, params={"cursor": "cursor-1", "limit": 100})

    assert response.json() == _PAGE.model_dump(mode="json")
    assert requests == [(None, 25), ("cursor-1", 100)]

    read_config = Mock(side_effect=AssertionError("config must not be read"))
    monkeypatch.setattr(runtime_env, "read_local_runtime_db_config", read_config)
    for resolved_user_id, params, expected_status in (
        ("user-1", {"limit": 0}, 422),
        ("user-1", {"limit": 101}, 422),
        ("other-user", {}, 403),
    ):
        response = _client(resolved_user_id).get(_STATE_PATH, params=params)
        assert response.status_code == expected_status
    read_config.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected_status", "boundary"),
    [
        (repository.ActionConversationNotFoundError("private"), 404, _READER),
        (cursor.OpaqueCursorError("private"), 422, _READER),
        (repository.ActionConversationCursorConflictError("private"), 409, _READER),
        (migrations.MigrationError("private migration"), 500, _READER),
        (sqlite3.DatabaseError("private database"), 500, _READER),
        (vector.SQLiteVectorExtensionError("private"), 500, "configure_connection"),
    ],
)
def test_action_state_maps_public_failures_without_private_details(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
    boundary: str,
) -> None:
    monkeypatch.setenv("LOCAL_DB_PATH", ":memory:")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1500")
    monkeypatch.setattr(action_router, boundary, Mock(side_effect=error))

    with pytest.raises((HTTPException, PublicAgentHTTPError)) as exc_info:
        action_router.action_state("user-1", "action-1", None, 25, "user-1")
    mapped = exc_info.value
    assert mapped.status_code == expected_status
    if isinstance(mapped, PublicAgentHTTPError):
        assert mapped.error_code == "ACTION_CONVERSATION_INTEGRITY_ERROR"
        assert mapped.public_message is None
    else:
        assert "private" not in str(mapped.detail)


@pytest.mark.asyncio
async def test_cancel_action_returns_accepted_when_action_is_already_terminal(
    monkeypatch: pytest.MonkeyPatch,
):
    delegated = AsyncMock(return_value=True)
    monkeypatch.setattr(action_router, "execute_action_cancel", delegated)

    response = await action_router.cancel_action(
        user_id="user-1",
        action_id="act-1",
        body=None,
        resolved_user_id="user-1",
    )

    assert response.status == "accepted"


@pytest.mark.asyncio
async def test_cancel_action_maps_incomplete_cleanup_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    delegated = AsyncMock(return_value=False)
    monkeypatch.setattr(action_router, "execute_action_cancel", delegated)

    with pytest.raises(HTTPException) as exc_info:
        await action_router.cancel_action(
            user_id="user-1",
            action_id="act-1",
            body=action_router.ActionCancelRequest(reason="user requested cancel"),
            resolved_user_id="user-1",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Action cancel cleanup incomplete"
    delegated.assert_awaited_once_with(
        user_id="user-1",
        action_id="act-1",
        reason="user requested cancel",
        expected_process_id=None,
    )
