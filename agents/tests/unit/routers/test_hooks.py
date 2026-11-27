"""The hooks list and the owner's switches over HTTP."""

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


def test_neither_builtins_nor_files_written_by_hand_are_listed_or_switched(
    client: TestClient, tasks: Path
) -> None:
    # Catches a built-in the owner can still switch off, and a file put in the
    # tasks folder by hand that is listed as a task or takes a switch.
    (tasks / "daily.md").write_text(TASK, encoding="utf-8")
    assert client.get(HOOKS).json()["hooks"] == []
    for hook_id in ("builtin.insight", "task.daily"):
        refused = client.put(f"{HOOKS}/{hook_id}/enabled", json={"enabled": False})
        assert (refused.status_code, refused.json()["detail"]["error_code"]) == (
            404,
            "HOOK_NOT_FOUND",
        )
