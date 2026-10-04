from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    apply_approval_decision,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import BrokerContext
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerPreflightOutcome,
    BrokerToolOutcome,
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedCommandRequest,
)
from pantaray_agents.local_runtime.tooling.brokering.command_approval_summaries import (
    build_bash_summary,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    OutsideWorkspaceGrantError,
    approved_summary_covers_request,
    grant_outside_workspace_folder_in_connection,
)
from pantaray_agents.local_runtime.tooling.workspace_manifest_roots import ManifestRoot
from pantaray_agents.schema.agent.base import JSONValue

from .action_seed import insert_agent_action
from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
    _register_folder_mount,
    _seed_broker_actor_process,
)
from .test_tool_broker_approval import _set_prompt_preference

APPROVAL_MODES = ("prompt_each_time", "always_allow")
TOOLS = ("read", "apply_patch", "bash", "run_python")


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Kept apart from tmp_path, which holds the app database.
    return tmp_path_factory.mktemp("outside-folder").resolve()


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[ValidatedCommandRequest]:
    requests: list[ValidatedCommandRequest] = []

    async def _capture(
        *, context: BrokerContext, request: ValidatedCommandRequest, **kwargs: object
    ) -> UnprojectedBrokerToolOutcome:
        del context, kwargs
        requests.append(request)
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={"status": "success", "exit_code": 0, "stdout": "", "stderr": ""},
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", _capture)
    return requests


def _setup(tmp_path: Path, mode: str) -> tuple[Path, ActionExecutionContext]:
    db_path, context = _bootstrap_runtime_db(tmp_path, allowed_tool_ids=TOOLS)
    if mode == "always_allow":
        _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
        _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    else:
        _set_prompt_preference(db_path)
    assert isinstance(context, ActionExecutionContext)
    return db_path, context


def _patch_args(path: Path) -> dict[str, JSONValue]:
    return {
        "changes": [
            {
                "op": "add",
                "path": str(path),
                "new_lines": ["created"],
                "trailing_newline": True,
            }
        ]
    }


def _bash_args(cwd: Path) -> dict[str, JSONValue]:
    return {"command": "touch made.txt", "cwd": str(cwd)}


async def _call(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    tool_id: str,
    request_id: str,
    args: dict[str, JSONValue],
    actor_process_id: str = BROKER_ACTOR_PROCESS_ID,
) -> BrokerPreflightOutcome | BrokerToolOutcome:
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id=tool_id,
        user_id="user-1",
        actor_process_id=actor_process_id,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id=f"invocation-{request_id}",
        tool_request_id=request_id,
        requested_at=BROKER_ACTOR_TIMESTAMP,
        args=args,
    )


def _allow_for_conversation(
    db_path: Path, error: BrokerApprovalRequiredError, request_id: str
) -> None:
    """Apply the decision the way the approval endpoint does in one transaction."""

    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id=request_id,
        user_id="user-1",
        action_id="action-1",
        approval_session_id=error.approval_session_id,
        decision="approved_once",
        decided_at=BROKER_ACTOR_TIMESTAMP,
    )
    with sqlite3.connect(db_path) as connection:
        with immediate_transaction(connection):
            grant_outside_workspace_folder_in_connection(
                connection,
                user_id="user-1",
                action_id="action-1",
                approval_session_id=error.approval_session_id,
                db_path=db_path,
                granted_at=BROKER_ACTOR_TIMESTAMP,
            )


def _summary(db_path: Path, request_id: str) -> dict[str, JSONValue]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT command_summary_json FROM approval_sessions WHERE tool_request_id = ?",
            (request_id,),
        ).fetchone()
    return json.loads(row[0])


def _approved_roots(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT manifest_id, canonical_real_path FROM workspace_manifest_roots "
            "WHERE source_type = 'approved_folder' ORDER BY manifest_id"
        ).fetchall()


async def _ask(
    db_path: Path,
    context: ActionExecutionContext,
    tool_id: str,
    request_id: str,
    args: dict[str, JSONValue],
) -> BrokerApprovalRequiredError:
    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _call(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id=request_id,
            args=args,
        )
    return asked.value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_conversation_grant_runs_the_call_and_opens_the_folder_for_later_calls(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    mode: str,
) -> None:
    db_path, context = _setup(tmp_path, mode)
    target = outside / "notes.txt"
    asked = await _ask(db_path, context, "apply_patch", "req-1", _patch_args(target))

    _allow_for_conversation(db_path, asked, "req-1")
    outcome = await _call(
        db_path=db_path,
        context=context,
        tool_id="apply_patch",
        request_id="req-1",
        args=_patch_args(target),
    )

    assert outcome.status == "success"
    assert target.read_text(encoding="utf-8") == "created\n"
    later_patch = _patch_args(outside / "second.txt")
    later_bash = _bash_args(outside)
    if mode == "always_allow":
        await _call(
            db_path=db_path,
            context=context,
            tool_id="apply_patch",
            request_id="req-2",
            args=later_patch,
        )
        await _call(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-3",
            args=later_bash,
        )
        assert (outside / "second.txt").is_file()
        [command] = launched
        assert str(outside) in command.real_write_roots
    else:
        await _ask(db_path, context, "apply_patch", "req-2", later_patch)
        await _ask(db_path, context, "bash", "req-3", later_bash)
        assert launched == []
    assert "outside_workspace" not in _summary(db_path, "req-2")
    assert "outside_workspace" not in _summary(db_path, "req-3")


@pytest.mark.asyncio
async def test_conversation_grant_from_a_command_runs_it_in_the_folder(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    asked = await _ask(
        db_path,
        context,
        "run_python",
        "req-1",
        {
            "code": "open('made.txt', 'w').write('x')",
            "args": [],
            "cwd": str(outside),
        },
    )

    _allow_for_conversation(db_path, asked, "req-1")
    outcome = await _call(
        db_path=db_path,
        context=context,
        tool_id="run_python",
        request_id="req-1",
        args={
            "code": "open('made.txt', 'w').write('x')",
            "args": [],
            "cwd": str(outside),
        },
    )

    assert outcome.status == "success"
    [command] = launched
    assert command.cwd == str(outside)
    assert str(outside) in command.real_write_roots


@pytest.mark.asyncio
async def test_grant_survives_a_new_run_and_gives_way_to_registration(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    db_path, context = _setup(tmp_path, "always_allow")
    asked = await _ask(db_path, context, "bash", "req-1", _bash_args(outside))
    _allow_for_conversation(db_path, asked, "req-1")

    def next_run(started_at: str) -> ActionExecutionContext:
        return ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at=started_at,
            allowed_tool_ids=TOOLS,
        )

    second = next_run("2026-03-24T00:00:00Z")
    await _call(
        db_path=db_path,
        context=second,
        tool_id="bash",
        request_id="req-2",
        args=_bash_args(outside),
    )
    assert _approved_roots(db_path) == [(context.manifest_id, str(outside))]

    _register_folder_mount(db_path=db_path, real_path=outside, display_name="Outside")
    third = next_run("2026-03-25T00:00:00Z")
    await _call(
        db_path=db_path,
        context=third,
        tool_id="bash",
        request_id="req-3",
        args=_bash_args(outside),
    )

    assert _approved_roots(db_path) == []
    assert len(launched) == 2
    assert all(str(outside) in command.real_write_roots for command in launched)


@pytest.mark.asyncio
async def test_conversation_grant_opens_every_folder_of_the_approval(
    tmp_path: Path,
    outside: Path,
    tmp_path_factory: pytest.TempPathFactory,
    launched: list[ValidatedCommandRequest],
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    second = tmp_path_factory.mktemp("second-outside-folder").resolve()
    asked = await _ask(db_path, context, "bash", "req-1", _bash_args(outside))
    two_folders = build_bash_summary(
        command="touch made.txt",
        cwd_relative_path=str(outside),
        timeout_ms=1_000,
        use_login_environment=False,
        run_outside_sandbox=False,
        reason=None,
        outside_workspace_folders=(outside, second),
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE approval_sessions SET command_summary_json = ? "
            "WHERE approval_session_id = ?",
            (json.dumps(two_folders), asked.approval_session_id),
        )

    _allow_for_conversation(db_path, asked, "req-1")

    assert sorted(_approved_roots(db_path)) == sorted(
        [(context.manifest_id, str(outside)), (context.manifest_id, str(second))]
    )
    await _call(
        db_path=db_path,
        context=context,
        tool_id="bash",
        request_id="req-2",
        args=_bash_args(second),
    )
    [command] = launched
    assert str(second) in command.real_write_roots
    assert "outside_workspace" not in _summary(db_path, "req-2")


@pytest.mark.asyncio
async def test_other_action_still_asks(tmp_path: Path, outside: Path) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    asked = await _ask(db_path, context, "bash", "req-1", _bash_args(outside))
    _allow_for_conversation(db_path, asked, "req-1")
    insert_agent_action(
        db_path=db_path, action_id="action-2", suggestion_id="suggestion-2"
    )
    _seed_broker_actor_process(
        db_path, process_id="process:action-2", action_id="action-2"
    )
    other = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-2",
        started_at=BROKER_ACTOR_TIMESTAMP,
        allowed_tool_ids=TOOLS,
    )

    with pytest.raises(BrokerApprovalRequiredError):
        await _call(
            db_path=db_path,
            context=other,
            tool_id="bash",
            request_id="req-other",
            args=_bash_args(outside),
            actor_process_id="process:action-2",
        )

    assert "outside_workspace" in _summary(db_path, "req-other")


@pytest.mark.asyncio
async def test_only_a_grantable_outside_folder_can_be_granted(
    tmp_path: Path,
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    # The parent of tmp_path encloses the app database folder.
    enclosing = tmp_path.parent
    enclosing_ask = await _ask(
        db_path, context, "apply_patch", "req-enclosing", _patch_args(enclosing / "x")
    )
    inside_ask = await _ask(
        db_path,
        context,
        "apply_patch",
        "req-inside",
        _patch_args(context.workspace_path / "a.txt"),
    )

    outside_summary = _summary(db_path, "req-enclosing")["outside_workspace"]
    assert isinstance(outside_summary, dict)
    assert outside_summary["can_allow_for_conversation"] is False
    for asked, request_id in (
        (enclosing_ask, "req-enclosing"),
        (inside_ask, "req-inside"),
    ):
        with pytest.raises(OutsideWorkspaceGrantError):
            _allow_for_conversation(db_path, asked, request_id)
    assert _approved_roots(db_path) == []


def _root(source_type: str, path: Path) -> ManifestRoot:
    return ManifestRoot(
        root_id=f"root:{source_type}:{path}",
        manifest_id="manifest:action-1",
        source_type=source_type,
        display_name=path.name,
        canonical_real_path=path,
        real_path=path,
        can_read=True,
        can_apply_patch=True,
        can_process_read=True,
        can_process_write=True,
    )


def test_registered_folder_root_still_covers_an_approved_outside_call(
    tmp_path: Path,
) -> None:
    """A folder registered while its approval waited replaces the grant at reconcile.

    The approved call must still match, through the registered folder root.
    """

    folder = tmp_path / "Reports"
    approved: dict[str, JSONValue] = {
        "summary_kind": "apply_patch",
        "target_paths": [str(folder / "q3.md")],
        "outside_workspace": {
            "folders": [{"path": str(folder), "display_name": "Reports"}],
            "can_allow_for_conversation": True,
        },
    }
    requested: dict[str, JSONValue] = {
        "summary_kind": "apply_patch",
        "target_paths": [str(folder / "q3.md")],
    }

    assert approved_summary_covers_request(
        approved=approved,
        requested=requested,
        manifest_roots=(_root("folder", folder),),
    )
    assert not approved_summary_covers_request(
        approved=approved,
        requested=requested,
        manifest_roots=(_root("folder", tmp_path / "Other"),),
    )


def test_an_approval_of_several_folders_covers_a_call_only_once_each_is_open(
    tmp_path: Path,
) -> None:
    first, second, other = tmp_path / "first", tmp_path / "second", tmp_path / "other"

    def summary(*folders: Path) -> dict[str, JSONValue]:
        return build_bash_summary(
            command="touch made.txt",
            cwd_relative_path=str(first),
            timeout_ms=1_000,
            use_login_environment=False,
            run_outside_sandbox=False,
            reason=None,
            outside_workspace_folders=folders,
        )

    approved = summary(first, second)
    both_open = (_root("approved_folder", first), _root("approved_folder", second))

    assert approved_summary_covers_request(
        approved=approved, requested=summary(), manifest_roots=both_open
    )
    # One approved folder is still outside and was never opened.
    assert not approved_summary_covers_request(
        approved=approved,
        requested=summary(),
        manifest_roots=(_root("approved_folder", first),),
    )
    # A folder registered meanwhile covers its part; the other is still asked.
    assert approved_summary_covers_request(
        approved=approved,
        requested=summary(second),
        manifest_roots=(_root("folder", first),),
    )
    # The call may not reach a folder the user never saw.
    assert not approved_summary_covers_request(
        approved=approved,
        requested=summary(second, other),
        manifest_roots=(_root("folder", first),),
    )
