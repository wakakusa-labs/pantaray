"""A TODO import's HTTP boundary over a migrated store."""

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
from pantaray_agents.routers import todo_imports as todo_imports_router
from pantaray_agents.routers.local.registry import register_local_routers

IMPORT = "6f1c2a52-7d0e-4c55-9f0e-1f2b3c4d5e6f"
IMPORTS = f"/v1/agents/users/{USER}/todo-imports"
ONE_IMPORT = f"{IMPORTS}/{IMPORT}"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(todo_imports_router, "read_todo_clock", lambda: NOW)
    register_logged_out_owner(USER)
    execute(
        path,
        f"""
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
          VALUES ('{USER}', 'ja', '{T}', '{T}');
        INSERT INTO workspace_organizations(organization_id, user_id, display_name,
          created_at, updated_at) VALUES ('org-a', '{USER}', 'A', '{T}', '{T}');
        INSERT INTO workspace_projects(project_id, user_id, display_name, created_at,
          updated_at) VALUES ('project-1', '{USER}', 'One', '{T}', '{T}');
        INSERT INTO workspace_project_organizations(user_id, project_id,
          organization_id, created_at) VALUES ('{USER}', 'project-1', 'org-a', '{T}');
        """,
    )
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER
    with TestClient(app) as test_client:
        yield test_client
    reset_logged_out_owner()


def _start(client: TestClient, **fields: object) -> Response:
    body: dict[str, object] = {
        "import_id": IMPORT,
        "scope": "all",
        "text": "Call the bank\nRenew the passport",
        **fields,
    }
    return client.post(IMPORTS, json=body)


def _draft(*groups: dict[str, object]) -> dict[str, object]:
    return {"groups": list(groups)}


def _group(
    organization: str | None, project: str | None, *texts: str
) -> dict[str, object]:
    return {
        "organization": organization,
        "project": project,
        "todos": [{"text": text} for text in texts],
    }


def _put(client: TestClient, revision: int, *groups: dict[str, object]) -> Response:
    return client.put(
        ONE_IMPORT, json={"expected_revision": revision, "draft": _draft(*groups)}
    )


def _refusal(response: Response) -> dict[str, object]:
    body = response.json()
    assert body["type"] == "TodoImportRefused", body
    return {key: value for key, value in body.items() if value is not None}


def test_a_start_opens_the_import_with_its_action_and_a_resend_returns_it(
    client: TestClient,
) -> None:
    started = _start(client)
    assert started.status_code == 200, started.text
    view = started.json()

    assert {key: view[key] for key in ("import_id", "scope", "revision", "state")} == {
        "import_id": IMPORT,
        "scope": "all",
        "revision": 1,
        "state": "open",
    }
    assert view["action_id"]
    assert _start(client).json() == view
    assert client.get(ONE_IMPORT).json() == view

    conflict = _start(client, text="Something else")
    assert conflict.status_code == 409
    assert _refusal(conflict) == {
        "type": "TodoImportRefused",
        "reason": "start_conflict",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"text": None},
        {"text": "  "},
        {"import_id": "import-1"},
        {"scope": "organization"},
        {"organization_id": "org-a"},
        {"files": [{"attachment_id": "x", "name": "a.txt", "byte_size": 1}]},
        {"user_id": "someone-else"},
    ],
)
def test_a_start_without_a_valid_body_is_refused_before_anything_opens(
    client: TestClient, fields: dict[str, object]
) -> None:
    assert _start(client, **fields).status_code == 422
    assert client.get(ONE_IMPORT).status_code == 404


def test_a_start_refuses_a_deleted_organization_and_a_missing_file(
    client: TestClient,
) -> None:
    gone = _start(client, scope="organization", organization_id="org-gone")
    missing = _start(
        client,
        import_id="00000000-0000-4000-8000-000000000002",
        files=[
            {
                "attachment_id": "00000000-0000-4000-8000-000000000001",
                "name": "minutes.txt",
                "byte_size": 4,
            }
        ],
    )

    assert gone.status_code == 422
    assert _refusal(gone)["reason"] == "organization_not_found"
    assert missing.status_code == 422
    assert _refusal(missing)["reason"] == "files_unavailable"


def test_the_path_user_must_be_the_token_user(client: TestClient) -> None:
    other = "/v1/agents/users/someone-else/todo-imports"

    assert (
        client.post(
            other, json={"import_id": IMPORT, "scope": "all", "text": "x"}
        ).status_code
        == 403
    )
    assert client.get(f"{other}/{IMPORT}").status_code == 403
    assert client.post(f"{other}/{IMPORT}/cancel").status_code == 403


def test_an_edit_returns_the_resolved_draft_or_says_why_it_was_refused(
    client: TestClient,
) -> None:
    _start(client, scope="organization", organization_id="org-a")

    written = _put(
        client, 1, _group(None, "One", "Call the bank"), _group("A", "New", "Plan")
    )
    stale = _put(client, 1, _group(None, "One", "Call the bank"))
    outside = _put(client, 2, _group("B", "Elsewhere", "Plan"))

    assert written.status_code == 200, written.text
    assert written.json()["revision"] == 2
    assert written.json()["groups"] == [
        {
            "organization": {"name": "A", "new": False},
            "project": {"name": "One", "new": False},
            "todos": [
                {
                    "text": "Call the bank",
                    "due": None,
                    "starts_on": None,
                    "duplicate": False,
                    "keep_duplicate": False,
                }
            ],
        },
        {
            "organization": {"name": "A", "new": False},
            "project": {"name": "New", "new": True},
            "todos": [
                {
                    "text": "Plan",
                    "due": None,
                    "starts_on": None,
                    "duplicate": False,
                    "keep_duplicate": False,
                }
            ],
        },
    ]
    assert stale.status_code == 409
    assert _refusal(stale)["reason"] == "stale_revision"
    assert outside.status_code == 422
    assert _refusal(outside) == {
        "type": "TodoImportRefused",
        "reason": "organization_outside_scope",
        "group_index": 0,
    }


def test_registering_creates_the_draft_once_and_closes_the_import(
    client: TestClient,
) -> None:
    _start(client)
    _put(client, 1, _group(None, None, "Renew the passport"))

    stale = client.post(f"{ONE_IMPORT}/register", json={"expected_revision": 1})
    registered = client.post(f"{ONE_IMPORT}/register", json={"expected_revision": 2})
    again = client.post(f"{ONE_IMPORT}/register", json={"expected_revision": 2})
    edit = _put(client, 2, _group(None, None, "Call the bank"))
    cancel = client.post(f"{ONE_IMPORT}/cancel")

    assert stale.status_code == 409
    assert _refusal(stale)["reason"] == "stale_revision"
    assert registered.status_code == 200, registered.text
    assert registered.json()["state"] == "registered"
    assert len(registered.json()["result"]["todo_ids"]) == 1
    assert again.json() == registered.json()
    assert (edit.status_code, _refusal(edit)["reason"]) == (409, "registered")
    assert (cancel.status_code, _refusal(cancel)["reason"]) == (409, "registered")


def test_a_canceled_import_takes_no_edit_and_an_unknown_one_is_not_found(
    client: TestClient,
) -> None:
    _start(client)

    canceled = client.post(f"{ONE_IMPORT}/cancel")
    edit = _put(client, 1, _group(None, None, "Call the bank"))
    unknown = client.get(f"{IMPORTS}/00000000-0000-4000-8000-000000000009")

    assert canceled.json()["state"] == "canceled"
    assert client.post(f"{ONE_IMPORT}/cancel").json() == canceled.json()
    assert (edit.status_code, _refusal(edit)["reason"]) == (409, "canceled")
    assert (unknown.status_code, _refusal(unknown)["reason"]) == (404, "not_found")
