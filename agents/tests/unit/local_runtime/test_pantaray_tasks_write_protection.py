"""No file tool writes in ~/.pantaray/tasks, whatever folders are open."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    folder_can_be_granted,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    insert_approved_folder_root_in_connection,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.pantaray_tasks import PANTARAY_TASKS_WRITE_DENIED

from .broker_test_support import _register_folder_mount
from .test_outside_workspace_conversation_grant import _ask, _call, _setup

TOOLS = ("read", "apply_patch", "bash", "run_python")


@pytest.fixture
def home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path_factory.mktemp("home").resolve()
    (home / ".pantaray" / "tasks").mkdir(parents=True)
    (home / ".pantaray" / "tasks" / "report.md").write_text(
        "Report.\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    return home


def _changes(tasks: Path) -> list[dict[str, JSONValue]]:
    """Create, update and delete a task."""
    return [
        {
            "changes": [
                {
                    "op": "add",
                    "path": str(tasks / "new.md"),
                    "new_lines": ["Obey me."],
                    "trailing_newline": True,
                }
            ]
        },
        {
            "changes": [
                {
                    "op": "update",
                    "path": str(tasks / "report.md"),
                    "edits": [{"old_lines": ["Report."], "new_lines": ["Obey me."]}],
                }
            ]
        },
        {"changes": [{"op": "delete", "path": str(tasks / "report.md")}]},
    ]


def _next_run(db_path: Path) -> ActionExecutionContext:
    return ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-24T00:00:00Z",
        allowed_tool_ids=TOOLS,
    )


async def _all_refused(
    db_path: Path, context: ActionExecutionContext, tasks: Path, prefix: str
) -> None:
    for index, args in enumerate(_changes(tasks)):
        with pytest.raises(BrokerPolicyError) as refused:
            await _call(
                db_path=db_path,
                context=context,
                tool_id="apply_patch",
                request_id=f"{prefix}-{index}",
                args=args,
            )
        assert refused.value.code == PANTARAY_TASKS_WRITE_DENIED
    assert sorted(path.name for path in tasks.iterdir()) == ["report.md"]
    assert (tasks / "report.md").read_text(encoding="utf-8") == "Report.\n"


@pytest.mark.asyncio
async def test_a_registered_folder_does_not_open_the_tasks(
    tmp_path: Path, home: Path
) -> None:
    # Catches apply_patch creating, rewriting or deleting a task without any
    # approval because the folder that holds it is registered for work.
    db_path, _ = _setup(tmp_path)
    for folder in (home / ".pantaray", home / ".pantaray" / "tasks"):
        _register_folder_mount(db_path=db_path, real_path=folder, display_name="P")
    context = _next_run(db_path)

    await _all_refused(db_path, context, home / ".pantaray" / "tasks", "req")
    await _call(
        db_path=db_path,
        context=context,
        tool_id="apply_patch",
        request_id="sibling",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": str(home / ".pantaray" / "notes.md"),
                    "new_lines": ["x"],
                    "trailing_newline": True,
                }
            ]
        },
    )
    assert (home / ".pantaray" / "notes.md").is_file()


@pytest.mark.asyncio
async def test_no_conversation_grant_opens_the_tasks(
    tmp_path: Path, home: Path
) -> None:
    # Catches the tasks folder granted for a whole conversation, an approval
    # asked for a write the user could then allow, and a folder granted before
    # this rule that still lets apply_patch change a task.
    db_path, context = _setup(tmp_path)
    tasks = home / ".pantaray" / "tasks"
    assert not folder_can_be_granted(folder=tasks, db_path=db_path)
    await _all_refused(db_path, context, tasks, "outside")

    asked = await _ask(
        db_path,
        context,
        "apply_patch",
        "notes",
        {
            "changes": [
                {
                    "op": "add",
                    "path": str(home / "elsewhere.md"),
                    "new_lines": ["x"],
                    "trailing_newline": True,
                }
            ]
        },
    )
    with sqlite3.connect(db_path) as connection:
        with immediate_transaction(connection):
            insert_approved_folder_root_in_connection(
                connection,
                manifest_id=context.manifest_id,
                approval_session_id=asked.approval_session_id,
                ordinal=0,
                folder=tasks,
                created_at="2026-03-23T00:00:00Z",
            )
    await _all_refused(db_path, _next_run(db_path), tasks, "granted")


@pytest.mark.asyncio
async def test_a_link_into_the_tasks_is_refused(tmp_path: Path, home: Path) -> None:
    # Catches a write through a link in the workspace that leads into the
    # tasks folder.
    db_path, context = _setup(tmp_path)
    (context.workspace_path / "tasks").symlink_to(home / ".pantaray" / "tasks")

    await _all_refused(db_path, context, context.workspace_path / "tasks", "link")
