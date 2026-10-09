"""The chat's HTTP boundary over a migrated store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.chat.store import (
    append_chat_item,
    chat_turn_end_message_id,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.routers import chat as chat_router
from pantaray_agents.routers.local.registry import register_local_routers
from pantaray_agents.schema.chat import TurnFailureContent

USER = "user-1"
MESSAGES = f"/v1/agents/users/{USER}/chat/messages"
ITEMS = f"/v1/agents/users/{USER}/chat/items"
RETRY = f"/v1/agents/users/{USER}/chat/turns/retry"
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
PAYLOAD = b"%PDF-1.7 staged"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    register_logged_out_owner(USER)
    yield path
    reset_logged_out_owner()


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    requested: list[tuple[str, str | None]] = []

    def request(user_id: str, *, retry_of: str | None = None) -> None:
        requested.append((user_id, retry_of))

    monkeypatch.setattr(chat_router, "request_chat_turn", request)
    return requested


@pytest.fixture
def client(db_path: Path, turns: list[tuple[str, str | None]]) -> Iterator[TestClient]:
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER
    with TestClient(app) as test_client:
        yield test_client


def _message(message_id: str = "m-1", **fields: object) -> dict[str, object]:
    return {"message_id": message_id, "text": "Hello", **fields}


def _pdf(byte_size: int = len(PAYLOAD)) -> dict[str, object]:
    return {"attachment_id": FILE_ID, "name": "Q3.pdf", "byte_size": byte_size}


def _row_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM chat_items").fetchone()[0])


def test_resending_a_message_id_returns_the_one_item(
    client: TestClient, db_path: Path
) -> None:
    first = client.post(MESSAGES, json=_message())
    again = client.post(MESSAGES, json=_message())
    changed = client.post(MESSAGES, json=_message(text="Something else"))

    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert first.json()["content"]["kind"] == "user_message"
    assert changed.status_code == 409
    assert changed.json() == {"type": "MessageIdentityConflict"}
    assert _row_count(db_path) == 1


def test_a_message_starts_a_turn_and_a_retry_runs_the_last_failed_one(
    client: TestClient, turns: list[tuple[str, str | None]]
) -> None:
    client.post(MESSAGES, json=_message())
    failed = [
        append_chat_item(
            user_id=USER,
            message_id=chat_turn_end_message_id(f"a{n}", "failure", 1),
            content=TurnFailureContent(kind="turn_failure", reason="llm_connection"),
        )
        for n in range(2)
    ]

    stale = client.post(RETRY, json={"failure_item_id": failed[0].item_id})
    latest = client.post(RETRY, json={"failure_item_id": failed[1].item_id})

    assert stale.status_code == 409
    assert stale.json() == {"type": "ChatTurnRetryStale"}
    assert latest.status_code == 204
    assert turns == [(USER, None), (USER, failed[1].item_id)]


def test_a_quote_must_name_an_item_of_this_chat(client: TestClient) -> None:
    quoted = client.post(MESSAGES, json=_message("m-1")).json()

    reply = client.post(MESSAGES, json=_message("m-2", quote_item_id=quoted["item_id"]))
    unknown = client.post(MESSAGES, json=_message("m-3", quote_item_id="missing"))

    assert reply.status_code == 200
    assert reply.json()["content"]["quote_item_id"] == quoted["item_id"]
    assert unknown.status_code == 400
    assert unknown.json() == {"type": "ChatMessageRejected", "field": "quote_item_id"}


def test_pages_run_newest_first_and_continue_from_the_cursor(
    client: TestClient,
) -> None:
    for index in range(3):
        client.post(MESSAGES, json=_message(f"m-{index}", text=f"text {index}"))

    first = client.get(ITEMS, params={"limit": 2}).json()
    rest = client.get(ITEMS, params={"limit": 2, "before": first["next_cursor"]})

    assert [item["content"]["text"] for item in first["items"]] == [
        "text 2",
        "text 1",
    ]
    assert [item["content"]["text"] for item in rest.json()["items"]] == ["text 0"]
    assert rest.json()["next_cursor"] is None


def test_another_users_chat_is_forbidden(client: TestClient) -> None:
    other = "/v1/agents/users/user-2/chat"

    assert client.post(f"{other}/messages", json=_message()).status_code == 403
    assert client.get(f"{other}/items").status_code == 403
    retry = {"failure_item_id": "f-1"}
    assert client.post(f"{other}/turns/retry", json=retry).status_code == 403


@pytest.mark.parametrize(
    ("fields", "rejected"),
    [
        ({"images": [{"storage_path": "user-2/2026-10-08/x.png"}]}, "images"),
        ({"files": [_pdf()]}, "files"),  # never staged
        ({"files": [{**_pdf(), "name": "run.exe"}]}, "files"),
        ({"files": [_pdf()] * 11}, "files"),
        ({"text": "   "}, "text"),
    ],
)
def test_attachments_and_text_follow_the_action_message_contract(
    client: TestClient,
    db_path: Path,
    fields: dict[str, object],
    rejected: str,
) -> None:
    response = client.post(MESSAGES, json=_message(**fields))

    assert response.status_code == 400
    assert response.json()["field"] == rejected
    assert _row_count(db_path) == 0


def test_a_staged_file_is_checked_and_left_in_place(
    client: TestClient, tmp_path: Path
) -> None:
    staged = tmp_path / f"artifacts/generated/attachments/{USER}/{FILE_ID}.pdf"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(PAYLOAD)

    wrong_size = client.post(MESSAGES, json=_message("m-1", files=[_pdf(1)]))
    accepted = client.post(MESSAGES, json=_message("m-2", files=[_pdf()]))

    assert wrong_size.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["content"]["files"] == [_pdf()]
    assert staged.read_bytes() == PAYLOAD


def test_items_cannot_be_rewritten_but_leave_with_their_user(
    client: TestClient, db_path: Path
) -> None:
    client.post(MESSAGES, json=_message())

    with closing(sqlite3.connect(db_path, autocommit=True)) as connection:
        configure_connection(connection, 1_000)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE chat_items SET payload = payload")
        connection.execute("DELETE FROM users WHERE user_id = ?", (USER,))

    assert _row_count(db_path) == 0


def test_a_named_project_follows_the_action_message_contract(
    client: TestClient,
) -> None:
    ref = {
        "project_id": "p-1",
        "display_name": "Aurora Web",
        "paths": ["/Users/me/aurora"],
        "start": 0,
        "end": 10,
    }
    sent = client.post(
        MESSAGES,
        json=_message(text="  Aurora Web の README を要約して", project_refs=[ref]),
    )
    # The span must name the project in the trimmed text, and paths are absolute.
    off_span = client.post(
        MESSAGES, json=_message("m-2", text="README of Aurora Web", project_refs=[ref])
    )
    relative = client.post(
        MESSAGES,
        json=_message(
            "m-3", text="Aurora Web", project_refs=[{**ref, "paths": ["aurora"]}]
        ),
    )

    assert sent.status_code == 200
    assert sent.json()["content"]["project_refs"] == [ref]
    stored = client.get(ITEMS).json()["items"][0]["content"]
    assert stored["project_refs"] == [ref]
    for rejected in (off_span, relative):
        assert rejected.status_code == 400
        assert rejected.json()["field"] == "project_refs"
