"""The TODO list's HTTP boundary over a migrated store."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from tests.unit.local_runtime.migrated_db import prepare_test_database
from tests.unit.local_runtime.todos.store_support import (
    NOW,
    USER,
    T,
    execute,
    seed_action,
)

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import (
    TodoClock,
)
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.writes import (
    set_todo_check,
    update_todo,
)
from pantaray_agents.routers import todos as todos_router
from pantaray_agents.routers.local.registry import register_local_routers

TODOS = f"/v1/agents/users/{USER}/todos"
# Wednesday 2026-10-28 in Tokyo, the week before NOW's.
PREVIOUS_WEEK = replace(NOW, now="2026-10-28T03:00:00.000Z")
# Exactly 30 days after a deletion at NOW.
AFTER_RETENTION = replace(NOW, now="2026-12-04T03:00:00.000Z")


class _Clock:
    def __init__(self) -> None:
        self.current: TodoClock = NOW

    def __call__(self) -> TodoClock:
        return self.current


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    register_logged_out_owner(USER)
    execute(
        path,
        f"""
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
          VALUES ('{USER}', 'ja', '{T}', '{T}');
        INSERT INTO workspace_projects(project_id, user_id, display_name, created_at,
          updated_at) VALUES ('project-1', '{USER}', 'One', '{T}', '{T}');
        """,
    )
    yield path
    reset_logged_out_owner()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fixed = _Clock()
    monkeypatch.setattr(todos_router, "read_todo_clock", fixed)
    return fixed


@pytest.fixture
def client(db: Path, clock: _Clock) -> Iterator[TestClient]:
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER
    with TestClient(app) as test_client:
        yield test_client


def _create(
    client: TestClient, client_todo_id: str = "c-1", **fields: object
) -> Response:
    body: dict[str, object] = {
        "client_todo_id": client_todo_id,
        "project_id": "project-1",
        "text": "Send the quote",
        "due": None,
        **fields,
    }
    return client.post(TODOS, json=body)


def _created_id(
    client: TestClient, client_todo_id: str = "c-1", **fields: object
) -> str:
    response = _create(client, client_todo_id, **fields)
    assert response.status_code == 200, response.text
    return str(response.json()["todo_id"])


def _error_code(response: Response) -> str:
    return str(response.json()["detail"]["error_code"])


def _list(client: TestClient, **params: str) -> Any:  # the JSON body
    response = client.get(TODOS, params={"scope": "all", "project": "any", **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_another_users_path_is_forbidden(client: TestClient) -> None:
    other = "/v1/agents/users/someone-else/todos"

    assert client.get(f"{other}?scope=all&project=any").status_code == 403
    assert client.delete(f"{other}/any-todo").status_code == 403


def test_a_replayed_create_returns_the_same_todo_and_a_changed_one_conflicts(
    client: TestClient,
) -> None:
    first = _create(client)
    replayed = _create(client)
    conflicting = _create(client, text="Send the invoice")

    assert first.status_code == replayed.status_code == 200
    assert replayed.json() == first.json()
    assert first.json()["author"] == "user"
    # No start date given: it starts today.
    assert first.json()["starts_on"] == "2026-11-04"
    assert conflicting.status_code == 409
    assert _error_code(conflicting) == "TODO_CLIENT_ID_CONFLICT"
    assert len(_list(client)["items"]) == 1


@pytest.mark.parametrize(
    ("fields", "status_code", "error_code"),
    [
        ({"project_id": "missing"}, 404, "PROJECT_NOT_FOUND"),
        ({"text": "two\nlines"}, 422, "TODO_INVALID_TEXT"),
        ({"text": "   "}, 422, "TODO_INVALID_TEXT"),
    ],
)
def test_a_create_the_store_refuses_names_why(
    client: TestClient, fields: dict[str, object], status_code: int, error_code: str
) -> None:
    response = _create(client, **fields)

    assert response.status_code == status_code
    assert _error_code(response) == error_code


def test_a_repeat_without_a_date_or_an_unknown_scope_is_an_input_error(
    client: TestClient,
) -> None:
    assert _create(client, due={"repeat": "weekly"}).status_code == 422
    for params in (
        {"scope": "organization", "project": "any"},
        {"scope": "all", "project": "any", "organization_id": "org-a"},
        {"scope": "all", "project": "project"},
    ):
        assert client.get(TODOS, params=params).status_code == 422


def test_the_user_removes_the_ais_check_and_checking_a_weekly_todo_adds_the_next(
    client: TestClient,
) -> None:
    todo_id = _created_id(client, due={"date": "2026-11-06", "repeat": "weekly"})
    checked = set_todo_check(
        user_id=USER, todo_id=todo_id, by="ai", checked=True, clock=NOW
    )
    assert isinstance(checked, core.CheckSet)

    unchecked = client.put(f"{TODOS}/{todo_id}/check", json={"checked": False})
    rechecked = client.put(f"{TODOS}/{todo_id}/check", json={"checked": True})

    assert unchecked.status_code == 200
    assert unchecked.json()["todo"]["state"] == {
        "kind": "not_started",
        "reason": "starting",
    }
    assert rechecked.json()["todo"]["state"]["by"] == "user"
    next_todo = rechecked.json()["next_todo"]
    assert next_todo["origin"] == "recurrence"
    assert next_todo["starts_on"] == "2026-11-11"
    assert next_todo["due"] == {"date": "2026-11-13", "repeat": "weekly"}


def test_the_list_sorts_by_due_and_a_todo_moved_to_tomorrow_waits_for_it(
    client: TestClient, clock: _Clock
) -> None:
    later = _created_id(client, "c-1", due={"date": "2026-11-20", "repeat": "none"})
    clock.current = replace(NOW, now="2026-11-04T03:00:01.000Z")
    sooner = _created_id(client, "c-2", due={"date": "2026-11-06", "repeat": "none"})

    moved = client.put(f"{TODOS}/{later}/start", json={"starts_on": "2026-11-05"})

    assert moved.status_code == 200, moved.text
    assert moved.json()["starts_on"] == "2026-11-05"
    assert moved.json()["state"] == {"kind": "not_started", "reason": "scheduled"}

    def listed(**params: str) -> list[str]:
        query = {"scope": "all", "project": "project", "project_id": "project-1"}
        response = client.get(TODOS, params={**query, **params})
        assert response.status_code == 200, response.text
        assert response.json()["today"] == "2026-11-04"
        return [item["todo_id"] for item in response.json()["items"]]

    assert listed() == [later, sooner]
    assert listed(sort="due") == [sooner, later]
    unknown = {"scope": "all", "project": "any", "sort": "name"}
    assert client.get(TODOS, params=unknown).status_code == 422


def test_a_todo_created_with_no_start_waits_until_the_user_sets_one(
    client: TestClient,
) -> None:
    created = _create(client, starts_on=None)
    todo_id = created.json()["todo_id"]

    assert created.status_code == 200, created.text
    assert created.json()["starts_on"] is None
    assert created.json()["state"] == {"kind": "not_started", "reason": "unscheduled"}
    assert _create(client, starts_on=None).json() == created.json()

    today = client.put(f"{TODOS}/{todo_id}/start", json={"starts_on": "2026-11-04"})
    unset = client.put(f"{TODOS}/{todo_id}/start", json={"starts_on": None})

    assert today.json()["state"] == {"kind": "not_started", "reason": "starting"}
    assert unset.status_code == 200, unset.text
    assert unset.json()["starts_on"] is None
    assert unset.json()["state"] == {"kind": "not_started", "reason": "unscheduled"}
    assert client.put(f"{TODOS}/{todo_id}/start", json={}).status_code == 422


def test_a_deleted_todo_refuses_check_and_due_but_deleting_again_is_fine(
    client: TestClient,
) -> None:
    todo_id = _created_id(client)

    first = client.delete(f"{TODOS}/{todo_id}")
    again = client.delete(f"{TODOS}/{todo_id}")
    check = client.put(f"{TODOS}/{todo_id}/check", json={"checked": True})
    due = client.put(f"{TODOS}/{todo_id}/due", json={"due": None})
    start = client.put(f"{TODOS}/{todo_id}/start", json={"starts_on": "2026-11-05"})

    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert (check.status_code, _error_code(check)) == (409, "TODO_DELETED")
    assert (due.status_code, _error_code(due)) == (409, "TODO_DELETED")
    assert (start.status_code, _error_code(start)) == (409, "TODO_DELETED")
    missing = client.delete(f"{TODOS}/no-such-todo")
    assert (missing.status_code, _error_code(missing)) == (404, "TODO_NOT_FOUND")


@pytest.mark.parametrize("run", ["running", "paused"])
def test_a_todo_whose_conversation_runs_or_waits_cannot_be_deleted(
    client: TestClient, db: Path, run: str
) -> None:
    todo_id = _created_id(client)
    seed_action(
        db, "a", message_id=todo_message_id(todo_id), status="processing", run=run
    )

    busy = client.delete(f"{TODOS}/{todo_id}")

    assert (busy.status_code, _error_code(busy)) == (409, "TODO_BUSY")
    execute(db, "UPDATE agent_actions SET status = 'success'")
    deleted = client.delete(f"{TODOS}/{todo_id}")
    assert deleted.status_code == 200
    assert deleted.json()["conversation"] == {"kind": "action", "action_id": "a"}


def test_approving_the_ais_deletion_request_waits_for_the_run_and_answers_once(
    client: TestClient, db: Path
) -> None:
    todo_id = _created_id(client)
    update_todo(
        user_id=USER,
        todo_id=todo_id,
        step=lambda todo: core.request_deletion(
            todo, request_id="r-1", reason="Duplicate", now=NOW.now
        ),
    )
    seed_action(
        db, "a", message_id=todo_message_id(todo_id), status="processing", run="running"
    )
    answer = f"{TODOS}/{todo_id}/deletion-requests/r-1/answer"

    assert _list(client)["items"][0]["deletion_request"] == {
        "request_id": "r-1",
        "reason": "Duplicate",
    }
    busy = client.post(answer, json={"answer": "approve"})
    kept = client.post(answer, json={"answer": "keep"})
    stale = client.post(answer, json={"answer": "approve"})

    assert (busy.status_code, _error_code(busy)) == (409, "TODO_BUSY")
    assert kept.status_code == 200
    assert kept.json()["deletion_request"] is None
    assert (stale.status_code, _error_code(stale)) == (409, "TODO_REQUEST_STALE")


def test_a_deleted_todo_is_listed_and_restorable_until_retention_ends(
    client: TestClient, clock: _Clock
) -> None:
    kept_id = _created_id(client, "c-kept")
    expired_id = _created_id(client, "c-expired")
    client.delete(f"{TODOS}/{kept_id}")
    client.delete(f"{TODOS}/{expired_id}")

    listed = client.get(f"{TODOS}/deleted", params={"scope": "all"})
    restored = client.post(f"{TODOS}/{kept_id}/restore")
    restored_again = client.post(f"{TODOS}/{kept_id}/restore")
    clock.current = AFTER_RETENTION
    expired = client.post(f"{TODOS}/{expired_id}/restore")

    assert listed.status_code == 200
    assert listed.json()[0] == {
        "todo_id": listed.json()[0]["todo_id"],
        "project_id": "project-1",
        "text": "Send the quote",
        "deleted_by": "user",
        "deleted_at": NOW.now,
        "purge_on": "2026-12-04",
    }
    assert restored.status_code == restored_again.status_code == 200
    assert restored_again.json() == restored.json()
    assert (expired.status_code, _error_code(expired)) == (
        409,
        "TODO_RESTORE_EXPIRED",
    )
    assert client.get(f"{TODOS}/deleted", params={"scope": "all"}).json() == []


def test_the_list_shows_each_todo_with_its_state_and_conversation(
    client: TestClient, db: Path, clock: _Clock
) -> None:
    clock.current = PREVIOUS_WEEK
    old_id = _created_id(client, "c-old", starts_on="2026-10-28")
    clock.current = NOW
    working_id = _created_id(client, "c-now")
    seed_action(
        db,
        "a",
        message_id=todo_message_id(working_id),
        status="processing",
        run="running",
    )

    page = _list(client)

    assert page["today"] == "2026-11-04"
    assert [
        (item["todo_id"], item["state"], item["conversation"]) for item in page["items"]
    ] == [
        (
            old_id,
            {"kind": "not_started", "reason": "starting"},
            {"kind": "none"},
        ),
        (
            working_id,
            {"kind": "working", "note": None},
            {"kind": "action", "action_id": "a"},
        ),
    ]
    assert page["items"][0]["starts_on"] == "2026-10-28"
