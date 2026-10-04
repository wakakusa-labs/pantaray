from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
    update_read_access_scope,
)

from .action_seed import insert_agent_action
from .broker_test_support import BROKER_ACTOR_PROCESS_ID, _seed_broker_actor_process
from .migrated_db import prepare_test_database

_USER_ID = "user-1"
_ACTION_ID = "action-1"
_NOW = "2026-03-23T00:00:00Z"


class ReadRuntimeContext(Protocol):
    manifest_id: str
    execution_session_id: str
    workspace_path: Path
    app_runtime_python: Path


class WorkspaceFolderRecord(Protocol):
    real_path: str


def bootstrap_read_runtime_db(
    tmp_path: Path,
    *,
    read_access_scope: str = "workspace",
) -> tuple[Path, ReadRuntimeContext]:
    # App storage lives apart from the files a test reads as the user's own.
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        read_access_scope=read_access_scope,
        now=_NOW,
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        action_id=_ACTION_ID,
        started_at=_NOW,
        allowed_tool_ids=("read", "render_pdf_page", "apply_patch", "bash"),
    )
    return db_path, context


def bootstrap_read_runtime_db_with_registered_folder(
    tmp_path: Path,
) -> tuple[Path, ReadRuntimeContext, WorkspaceFolderRecord]:
    # App storage lives apart from the files a test reads as the user's own.
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        read_access_scope="workspace",
        now=_NOW,
    )
    repo = tmp_path / "repo-a"
    repo.mkdir()
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        real_path=repo,
        display_name="Repo A",
        organization_ids=(),
        project_ids=(),
        now="2026-03-23T00:00:01Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=_USER_ID,
        action_id=_ACTION_ID,
        started_at=_NOW,
        allowed_tool_ids=("read", "render_pdf_page", "apply_patch", "bash"),
    )
    return db_path, context, folder


async def execute_read_tool(
    *,
    db_path: Path,
    context: ReadRuntimeContext,
    args: dict[str, object],
    invocation_id: str | None = None,
    tool_request_id: str | None = None,
):
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="read",
        user_id=_USER_ID,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args=args,
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
    )
