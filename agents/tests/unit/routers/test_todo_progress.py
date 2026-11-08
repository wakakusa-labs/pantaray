"""Goals, progress records and the progress view over HTTP, on a migrated store."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from tests.unit.local_runtime.migrated_db import prepare_test_database
from tests.unit.local_runtime.todos.store_support import NOW, USER, T, execute

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.routers import todo_progress as progress_router
from pantaray_agents.routers import todos as todos_router
from pantaray_agents.routers.local.registry import register_local_routers

TODOS = f"/v1/agents/users/{USER}/todos"
GOAL = f"{TODOS}/projects/project-1/goal"
GOAL_BODY = {
    "text": "Close 300M yen",
    "target": 300_000_000,
    "unit": "円",
    "deadline": "2026-12-31",
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    for router in (progress_router, todos_router):
        monkeypatch.setattr(router, "read_todo_clock", lambda: NOW)
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
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER
    with TestClient(app) as test_client:
        yield test_client
    reset_logged_out_owner()


def _todo_id(client: TestClient) -> str:
    response = client.post(
        TODOS,
        json={
            "client_todo_id": "c-1",
            "project_id": "project-1",
            "text": "Close the Acme deal",
            "week_start": "2026-11-04",
            "due": None,
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["todo_id"])


def _record(
    client: TestClient,
    todo_id: str,
    quantity: float = 30_000_000,
    client_record_id: str = "r-1",
) -> Response:
    return client.post(
        f"{TODOS}/{todo_id}/progress-records",
        json={
            "client_record_id": client_record_id,
            "quantity": quantity,
            "unit": "円",
            "note": "Acme signed",
        },
    )


def _progress(client: TestClient, **query: str) -> Response:
    params = {
        "scope": "all",
        "project": "project",
        "project_id": "project-1",
        "period": "week",
        **query,
    }
    return client.get(f"{TODOS}/progress", params=params)


def _error_code(response: Response) -> str:
    return str(response.json()["detail"]["error_code"])


def test_a_goal_and_a_users_record_show_as_measured_progress(
    client: TestClient,
) -> None:
    assert _progress(client).json()["goals"] == [
        {"project_id": "project-1", "kind": "no_goal"}
    ]
    assert client.put(GOAL, json=GOAL_BODY).status_code == 200
    todo_id = _todo_id(client)
    record = _record(client, todo_id)
    assert record.status_code == 200, record.text
    assert _record(client, todo_id).json() == record.json()
    assert (
        _error_code(_record(client, todo_id, 1)) == "TODO_PROGRESS_CLIENT_ID_CONFLICT"
    )

    view = _progress(client).json()

    record_id = record.json()["record_id"]
    (goal,) = view["goals"]
    (this_week,) = [row for row in view["rows"] if row["start"] == "2026-11-02"]
    assert (goal["kind"], goal["total"], goal["share"]) == ("measured", 3e7, 0.1)
    assert goal["record_ids"] == this_week["record_ids"] == [record_id]
    assert view["records"][0]["evidence"] == {"kind": "user"}

    deleted = client.delete(f"{TODOS}/progress-records/{record_id}")
    assert deleted.status_code == 204
    assert _progress(client).json()["goals"][0]["kind"] == "no_records"
    assert client.delete(GOAL).status_code == 204
    assert client.delete(GOAL).status_code == 204


def test_a_pace_too_slow_to_date_is_still_a_view(client: TestClient) -> None:
    client.put(GOAL, json=GOAL_BODY)
    todo_id = _todo_id(client)
    for client_record_id in ("r-1", "r-2", "r-3"):
        assert _record(client, todo_id, 1, client_record_id).status_code == 200

    response = _progress(client)

    assert response.status_code == 200, response.text
    (goal,) = response.json()["goals"]
    assert (goal["total"], goal["pace"]["kind"]) == (3.0, "beyond_horizon")
    assert "projected_on" not in goal["pace"]


def test_any_month_or_year_is_a_page_and_the_goal_counts_to_today(
    client: TestClient,
) -> None:
    client.put(GOAL, json=GOAL_BODY)
    record_id = _record(client, _todo_id(client)).json()["record_id"]

    current = _progress(client).json()
    october = _progress(client, period="day", on="2026-10-15").json()
    year = _progress(client, period="month", on="2026-02-28").json()

    assert (current["page_start"], current["today"]) == ("2026-11-01", "2026-11-04")
    rows = october["rows"]
    assert (october["page_start"], october["today"], len(rows)) == (
        "2026-10-01",
        "2026-11-04",
        35,
    )
    assert (rows[0]["start"], rows[-1]["end"]) == ("2026-09-28", "2026-11-01")
    assert all(row["record_ids"] == [] for row in rows)
    assert year["page_start"] == "2026-01-01"
    assert [row["start"] for row in year["rows"]][::11] == ["2026-01-01", "2026-12-01"]
    assert year["rows"][10]["record_ids"] == [record_id]
    assert october["goals"] == year["goals"] == current["goals"]
    assert october["records"] == current["records"]


@pytest.mark.parametrize(
    "query",
    [
        {"on": "1969-12-31"},
        {"on": "9999-01-01"},
        {"on": "2026-02-30"},
        {"on": "2026-11"},
        {"period": "year"},
    ],
)
def test_a_page_outside_the_calendar_is_refused(
    client: TestClient, query: dict[str, str]
) -> None:
    assert _progress(client, **query).status_code == 422


def test_refusals_carry_error_codes(client: TestClient) -> None:
    other_project = client.put(f"{TODOS}/projects/missing/goal", json=GOAL_BODY)
    early = client.put(GOAL, json={**GOAL_BODY, "deadline": "2026-11-03"})
    missing_todo = _record(client, "missing")
    bad_query = _progress(client, project="any")

    assert (other_project.status_code, _error_code(other_project)) == (
        404,
        "PROJECT_NOT_FOUND",
    )
    assert (early.status_code, _error_code(early)) == (
        422,
        "TODO_GOAL_DEADLINE_BEFORE_SET",
    )
    assert (missing_todo.status_code, _error_code(missing_todo)) == (
        404,
        "TODO_NOT_FOUND",
    )
    assert bad_query.status_code == 422
    assert (
        client.get(
            "/v1/agents/users/someone-else/todos/progress",
            params={"scope": "all", "project": "any", "period": "day"},
        ).status_code
        == 403
    )
