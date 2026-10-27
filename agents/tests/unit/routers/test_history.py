from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.local_runtime.action_conversation import cursor_codec as cursor
from pantaray_agents.local_runtime.runtime import runtime_env
from pantaray_agents.local_runtime.storage import migrations
from pantaray_agents.local_runtime.storage import sqlite_vector as vector
from pantaray_agents.routers import history as history_router
from pantaray_agents.schema import conversation_history as history
from pantaray_agents.utils.error_handling import public_agent_http_error_handler

_PAGE = history.ConversationHistoryPage(
    items=(
        history.ConversationHistoryItem(
            kind="conversation",
            action_id="action-1",
            title="USER request",
            updated_at="2026-08-30T00:00:00.000Z",
            status="idle",
            latest_completion_event_id="event-1",
            status_line="Done",
            artifact_names=("report.md",),
        ),
    ),
    next_cursor="next-page",
)
_PATH = "/api/agent/history"
_READER = "read_conversation_history_page_in_connection"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(history_router.router)
    app.dependency_overrides[history_router.get_current_user_id_from_token] = lambda: (
        "user-123"
    )
    return TestClient(app)


def test_history_reads_configured_snapshot_and_returns_strict_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[object, ...]] = []

    def read_page(**kwargs: object) -> history.ConversationHistoryPage:
        connection = kwargs["connection"]
        assert isinstance(connection, sqlite3.Connection)
        assert connection.in_transaction
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1_500
        assert (
            connection.execute(
                "SELECT pantaray_conversation_history_casefold(?)", ("CAFÉ",)
            ).fetchone()[0]
            == "café"
        )
        requests.append(
            (
                kwargs["user_id"],
                kwargs["cursor"],
                kwargs["limit"],
                kwargs["search_text"],
            )
        )
        return _PAGE

    monkeypatch.setenv("LOCAL_DB_PATH", ":memory:")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1500")
    monkeypatch.setattr(history_router, _READER, read_page)

    with _client() as client:
        default_response = client.get(_PATH, headers={"Authorization": "Bearer token"})
        emoji_response = client.get(
            _PATH,
            params={
                "cursor": "cursor-1",
                "limit": 100,
                "search_text": "😀" * 256,
            },
            headers={"Authorization": "Bearer token"},
        )

    assert default_response.json() == _PAGE.model_dump(mode="json")
    assert emoji_response.status_code == 200
    assert requests == [
        ("user-123", None, 25, ""),
        ("user-123", "cursor-1", 100, "😀" * 256),
    ]


def test_history_item_reads_one_row_or_answers_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[object, object]] = []

    def read_item(**kwargs: object) -> history.ConversationHistoryListItem | None:
        connection = kwargs["connection"]
        assert isinstance(connection, sqlite3.Connection)
        assert connection.in_transaction
        requests.append((kwargs["user_id"], kwargs["item"]))
        return (
            _PAGE.items[0] if kwargs["item"] == ("conversation", "action-1") else None
        )

    monkeypatch.setenv("LOCAL_DB_PATH", ":memory:")
    monkeypatch.setattr(
        history_router, "read_conversation_history_item_in_connection", read_item
    )

    with _client() as client:
        found = client.get(f"{_PATH}/items/conversation/action-1")
        missing = client.get(f"{_PATH}/items/suggestion/gone")
        bad_kind = client.get(f"{_PATH}/items/folder/x")

    assert found.json() == _PAGE.items[0].model_dump(mode="json")
    assert missing.status_code == 404
    assert bad_kind.status_code == 422
    assert requests == [
        ("user-123", ("conversation", "action-1")),
        ("user-123", ("suggestion", "gone")),
    ]


def test_history_rejects_invalid_query_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_config = Mock(side_effect=AssertionError("database must not be opened"))
    monkeypatch.setattr(runtime_env, "read_local_runtime_db_config", read_config)

    with _client() as client:
        for params in (
            {"search_text": "😀" * 257},
            {"limit": 0},
            {"limit": 101},
        ):
            response = client.get(
                _PATH,
                params=params,
                headers={"Authorization": "Bearer token"},
            )
            assert response.status_code == 422
    read_config.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected_status", "boundary"),
    [
        (cursor.OpaqueCursorError("private cursor"), 422, _READER),
        (migrations.MigrationError("private migration"), 500, _READER),
        (sqlite3.DatabaseError("private database"), 500, _READER),
        (
            vector.SQLiteVectorExtensionError("private vector"),
            500,
            "configure_connection",
        ),
    ],
)
def test_history_maps_fixed_public_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
    boundary: str,
) -> None:
    monkeypatch.setenv("LOCAL_DB_PATH", ":memory:")
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1500")
    monkeypatch.setattr(history_router, boundary, Mock(side_effect=error))

    with pytest.raises((HTTPException, PublicAgentHTTPError)) as exc_info:
        history_router.list_conversation_history(
            cursor=None,
            limit=25,
            search_text="",
            user_id="user-123",
        )
    mapped = exc_info.value
    assert mapped.status_code == expected_status
    assert "private" not in str(mapped)
    if isinstance(mapped, PublicAgentHTTPError):
        assert mapped.error_code == "CONVERSATION_HISTORY_INTEGRITY_ERROR"
        assert mapped.public_message is None


def test_delete_history_item_returns_204_and_maps_busy_and_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outcomes = [None, history_router.ConversationBusyError("private"), sqlite3.Error()]
    delete = Mock(side_effect=outcomes)
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1500")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(history_router, "delete_history_item", delete)
    client = _client()
    client.app.add_exception_handler(  # type: ignore[attr-defined]
        PublicAgentHTTPError, public_agent_http_error_handler
    )

    with client:
        responses = [
            client.delete(f"{_PATH}/items/{kind}/item-1")
            for kind in ("conversation", "suggestion", "conversation", "process")
        ]

    assert [response.status_code for response in responses] == [204, 409, 500, 422]
    assert responses[0].content == b""
    assert responses[1].json()["detail"]["error_code"] == "CONVERSATION_BUSY"
    assert "private" not in responses[1].text
    assert responses[2].json()["detail"]["error_code"] == (
        "CONVERSATION_HISTORY_DELETE_FAILED"
    )
    assert [call.kwargs["kind"] for call in delete.call_args_list] == [
        "conversation",
        "suggestion",
        "conversation",
    ]
    assert delete.call_args.kwargs["user_id"] == "user-123"
