from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from ..migrated_db import prepare_test_database
from .store_support import USER, T, execute


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    register_logged_out_owner(USER)
    execute(
        db_path,
        f"""
        INSERT INTO users(user_id, ui_language, created_at, updated_at)
          VALUES ('{USER}', 'ja', '{T}', '{T}');
        INSERT INTO workspace_organizations(organization_id, user_id, display_name,
          created_at, updated_at) VALUES
          ('org-a', '{USER}', 'A', '{T}', '{T}'), ('org-b', '{USER}', 'B', '{T}', '{T}');
        INSERT INTO workspace_projects(project_id, user_id, display_name, created_at,
          updated_at) VALUES ('project-1', '{USER}', 'One', '{T}', '{T}');
        INSERT INTO workspace_project_organizations(user_id, project_id,
          organization_id, created_at) VALUES ('{USER}', 'project-1', 'org-a', '{T}');
        """,
    )
    yield db_path
    reset_logged_out_owner()
