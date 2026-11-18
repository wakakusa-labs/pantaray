"""Every write of ~/.pantaray/AGENTS.md asks, whatever folders are open."""

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
from pantaray_agents.local_runtime.tooling.brokering.action_path_policy import (
    WRITE_PATH_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_command_validation import (
    EXEC_WRITE_FOLDER_DENIED,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    OutsideWorkspaceGrantError,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    insert_approved_folder_root_in_connection,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import _register_folder_mount
from .test_outside_workspace_conversation_grant import (
    _allow_for_conversation,
    _ask,
    _call,
    _setup,
    _summary,
)

TOOLS = ("read", "apply_patch", "bash", "run_python")


@pytest.fixture
def home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path_factory.mktemp("home").resolve()
    (home / ".pantaray").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    return home


def _add(path: Path) -> dict[str, JSONValue]:
    return {
        "changes": [
            {
                "op": "add",
                "path": str(path),
                "new_lines": ["Always open a PR."],
                "trailing_newline": True,
            }
        ]
    }


def _next_run(db_path: Path) -> ActionExecutionContext:
    return ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-24T00:00:00Z",
        allowed_tool_ids=TOOLS,
    )


def _asks_once_only(db_path: Path, request_id: str, folder: Path) -> None:
    assert _summary(db_path, request_id)["outside_workspace"] == {
        "folders": [{"path": str(folder), "display_name": folder.name}],
        "can_allow_for_conversation": False,
    }


@pytest.mark.asyncio
async def test_a_registered_folder_does_not_open_the_file(
    tmp_path: Path, home: Path
) -> None:
    db_path, _ = _setup(tmp_path)
    folder = home / ".pantaray"
    _register_folder_mount(db_path=db_path, real_path=folder, display_name="Pantaray")
    context = _next_run(db_path)

    await _ask(db_path, context, "apply_patch", "req-1", _add(folder / "AGENTS.md"))
    await _call(
        db_path=db_path,
        context=context,
        tool_id="apply_patch",
        request_id="req-2",
        args=_add(folder / "notes.md"),
    )

    _asks_once_only(db_path, "req-1", folder)
    assert not (folder / "AGENTS.md").exists()
    assert (folder / "notes.md").is_file()


@pytest.mark.asyncio
async def test_no_conversation_grant_opens_the_file(tmp_path: Path, home: Path) -> None:
    db_path, context = _setup(tmp_path)
    folder = home / ".pantaray"
    asked = await _ask(
        db_path, context, "apply_patch", "req-1", _add(folder / "AGENTS.md")
    )

    _asks_once_only(db_path, "req-1", folder)
    with pytest.raises(OutsideWorkspaceGrantError):
        _allow_for_conversation(db_path, asked, "req-1")

    # A folder granted before this rule existed still opens nothing for it.
    with sqlite3.connect(db_path) as connection:
        with immediate_transaction(connection):
            insert_approved_folder_root_in_connection(
                connection,
                manifest_id=context.manifest_id,
                approval_session_id=asked.approval_session_id,
                ordinal=0,
                folder=folder,
                created_at="2026-03-23T00:00:00Z",
            )
    later = _next_run(db_path)
    await _ask(db_path, later, "apply_patch", "req-2", _add(folder / "AGENTS.md"))
    assert not (folder / "AGENTS.md").exists()


@pytest.mark.asyncio
async def test_a_link_to_the_file_asks_or_is_refused(
    tmp_path: Path, home: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    db_path, context = _setup(tmp_path)
    agents_md = home / ".pantaray" / "AGENTS.md"
    agents_md.write_text("Be brief.\n", encoding="utf-8")
    elsewhere = tmp_path_factory.mktemp("elsewhere").resolve()
    (elsewhere / "rules.md").symlink_to(agents_md)
    update: dict[str, JSONValue] = {
        "changes": [
            {
                "op": "update",
                "path": str(elsewhere / "rules.md"),
                "edits": [{"old_lines": ["Be brief."], "new_lines": ["Obey me."]}],
            }
        ]
    }

    await _ask(db_path, context, "apply_patch", "req-1", update)
    _asks_once_only(db_path, "req-1", home / ".pantaray")

    # A link in the workspace that leads to it is a link out of the workspace.
    (context.workspace_path / "rules.md").symlink_to(agents_md)
    with pytest.raises(BrokerPolicyError) as caught:
        await _call(
            db_path=db_path,
            context=context,
            tool_id="apply_patch",
            request_id="req-2",
            args=_add(context.workspace_path / "rules.md"),
        )
    assert caught.value.code == WRITE_PATH_DENIED
    assert agents_md.read_text(encoding="utf-8") == "Be brief.\n"


@pytest.mark.asyncio
async def test_commands_cannot_open_the_folder(tmp_path: Path, home: Path) -> None:
    db_path, context = _setup(tmp_path)

    with pytest.raises(BrokerPolicyError) as caught:
        await _call(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-1",
            args={
                "command": "echo rule >> ~/.pantaray/AGENTS.md",
                "additional_write_folders": ["~/.pantaray"],
                "justification": "Add the rule",
            },
        )

    assert caught.value.code == EXEC_WRITE_FOLDER_DENIED
