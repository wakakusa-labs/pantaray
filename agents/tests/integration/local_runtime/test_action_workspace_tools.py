from __future__ import annotations

import sqlite3
from functools import partial
from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _bootstrap_runtime_db_with_registered_folder,
    _grant_workspace_full_access,
)
from tests.unit.local_runtime.test_action_subagent_command_sandbox import (
    _seed_children_and_claim,
)

from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool

from .support import SEATBELT_SKIP_REASON, seatbelt_available

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


@pytest.mark.asyncio
async def test_python_copies_to_other_authorized_root_and_denies_outside(
    tmp_path: Path, outside_temp_path: Path
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path, allowed_tool_ids=("read", "apply_patch", "bash", "run_python")
    )
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    source = context.workspace_path / "report.md"
    source.write_text("verified report\n")
    execute = partial(
        execute_broker_tool,
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_id="run_python",
    )
    for index, destination in enumerate(
        (repo / "report.md", outside_temp_path / "outside.md")
    ):
        outcome = await execute(
            tool_request_id=f"copy-{index}",
            args={
                "code": f"import shutil; shutil.copyfile({str(source)!r}, {str(destination)!r})"
            },
        )
        if index == 0:
            assert outcome.status == "success", outcome.output
            assert destination.read_text() == source.read_text()
        else:
            assert outcome.status == "error", outcome.output
            assert not destination.exists()


@pytest.mark.asyncio
async def test_parent_can_read_during_child_claim_but_only_child_can_write(
    outside_temp_path: Path,
) -> None:
    # A claim narrows the write roots; a temp root above the workspace would not.
    db_path, context = _bootstrap_runtime_db(
        outside_temp_path, allowed_tool_ids=("bash", "run_python")
    )
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    claimed = context.workspace_path / "claimed"
    claimed.mkdir()
    target = claimed / "owned.txt"
    target.write_text("original")
    _seed_children_and_claim(
        db_path=db_path, manifest_id=context.manifest_id, claimed_path=claimed
    )
    execute = partial(
        execute_broker_tool,
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
    )
    read = await execute(
        tool_id="bash",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        tool_request_id="parent-read",
        args={"command": "pwd"},
    )
    assert read.status == "success", read.output
    assert read.output["stdout"].strip() == str(context.workspace_path)
    content = await execute(
        tool_id="run_python",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        tool_request_id="parent-read-content",
        args={
            "code": "from pathlib import Path; print(Path('claimed/owned.txt').read_text())"
        },
    )
    assert content.status == "success", content.output
    assert content.output["stdout"].strip() == "original"
    for actor in (BROKER_ACTOR_PROCESS_ID, "child-unclaimed", "child-owned"):
        outcome = await execute(
            tool_id="run_python",
            actor_process_id=actor,
            tool_request_id=f"write-{actor}",
            args={
                "code": "from pathlib import Path; Path('claimed/owned.txt').write_text('changed')"
            },
        )
        assert outcome.status == ("success" if actor == "child-owned" else "error"), (
            outcome.output
        )
        assert target.read_text() == (
            "changed" if actor == "child-owned" else "original"
        )


@pytest.mark.asyncio
async def test_file_and_command_tools_share_session_cwd(tmp_path: Path) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    for capability in ("scoped_write", "process_exec_local"):
        _grant_workspace_full_access(
            db_path=db_path, manifest_id=context.manifest_id, capability=capability
        )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE execution_sessions SET cwd_path = ? WHERE execution_session_id = ?",
            (str(repo), context.execution_session_id),
        )
    execute = partial(
        execute_broker_tool,
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
    )
    patch = await execute(
        tool_id="apply_patch",
        tool_request_id="create-report",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": "report.md",
                    "new_lines": ["in repository"],
                    "trailing_newline": True,
                }
            ]
        },
    )
    assert patch.output["applied_paths"] == [str(repo / "report.md")]
    assert not (context.workspace_path / "report.md").exists()
    read = await execute(
        tool_id="read", tool_request_id="read-report", args={"path": "report.md"}
    )
    assert read.status == "success", read.output
    command = await execute(
        tool_id="bash", tool_request_id="pwd-repo", args={"command": "pwd"}
    )
    assert command.status == "success", command.output
    assert command.output["stdout"].strip() == str(repo)


@pytest.mark.asyncio
async def test_broker_executes_pytest_from_nested_project_environment(
    tmp_path: Path,
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    (repo / ".git").mkdir()
    project = repo / "agents"
    executable = project / ".venv/bin/pytest"
    executable.parent.mkdir(parents=True)
    # A real executable proves the broker -> shebang -> OS launch path.
    executable.write_text("#!/bin/sh\nprintf 'nested pytest entrypoint\\n'\n")
    executable.chmod(0o755)
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_id="bash",
        tool_request_id="nested-pytest",
        args={"command": "pytest", "cwd": str(project)},
    )
    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"] == "nested pytest entrypoint\n"
