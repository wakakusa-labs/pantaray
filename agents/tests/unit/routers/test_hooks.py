"""The hooks list and the owner's approvals and switches over HTTP."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.unit.local_runtime.migrated_db import prepare_test_database

from pantaray_agents.app.shared import install_common_exception_handlers
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.routers.local.registry import register_local_routers

USER = "user-1"
HOOKS = f"/v1/agents/users/{USER}/hooks"
TASK = """+++
trigger = { every = "day", tz = "utc" }
conversation = "new"
+++
Sum up yesterday.
"""


@pytest.fixture
def tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home" / ".pantaray" / "tasks"
    path.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "runtime.db"
    prepare_test_database(db, 1_000, load_default_migrations())
    monkeypatch.setenv("LOCAL_DB_PATH", str(db))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    app = FastAPI()
    register_local_routers(app)
    install_common_exception_handlers(app)
    app.dependency_overrides[get_current_user_id_from_token] = lambda: USER
    with TestClient(app) as client:
        yield client


def _task(client: TestClient) -> dict[str, object]:
    (task,) = (
        hook for hook in client.get(HOOKS).json()["hooks"] if hook["kind"] == "task"
    )
    return task


def test_the_owner_approves_the_content_they_read(
    client: TestClient, tasks: Path
) -> None:
    # Catches an approval that takes whatever the file holds now, so a change
    # made after the owner read it would be approved unseen.
    (tasks / "daily.md").write_text(TASK, encoding="utf-8")
    seen = _task(client)
    assert (seen["hook_id"], seen["approved"], seen["text"]) == (
        "task.daily",
        False,
        TASK,
    )

    (tasks / "daily.md").write_text(TASK.replace("yesterday", "the week"))
    stale = client.put(
        f"{HOOKS}/task.daily/approval", json={"content_hash": seen["content_hash"]}
    )
    assert (stale.status_code, stale.json()["detail"]["error_code"]) == (
        409,
        "HOOK_TASK_CHANGED",
    )
    assert _task(client)["approved"] is False

    current = _task(client)
    assert (
        client.put(
            f"{HOOKS}/task.daily/approval",
            json={"content_hash": current["content_hash"]},
        ).status_code
        == 204
    )
    assert _task(client)["approved"] is True


def test_only_tasks_are_approved_and_only_known_hooks_switched(
    client: TestClient, tasks: Path
) -> None:
    # Catches a built-in approved as if a task, and a switch stored for a hook
    # that does not exist.
    builtin = client.put(
        f"{HOOKS}/builtin.insight/approval", json={"content_hash": "0" * 64}
    )
    assert builtin.json()["detail"]["error_code"] == "HOOK_NOT_A_TASK"
    missing = client.put(f"{HOOKS}/task.gone/enabled", json={"enabled": False})
    assert missing.status_code == 404

    assert (
        client.put(
            f"{HOOKS}/builtin.insight/enabled", json={"enabled": False}
        ).status_code
        == 204
    )
    (insight,) = (
        hook
        for hook in client.get(HOOKS).json()["hooks"]
        if hook["hook_id"] == "builtin.insight"
    )
    assert insight["enabled"] is False
