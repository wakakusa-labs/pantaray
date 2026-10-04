"""bash and run_python asking to write folders outside the workspace."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    WorkspacePathResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
from pantaray_agents.local_runtime.tooling.brokering.action_subagent_broker_authority import (
    ACTION_SUBAGENT_WRITE_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BROKER_TOOL_ARGS_INVALID,
    BrokerApprovalRequiredError,
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_command_validation import (
    EXEC_WRITE_FOLDER_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import BrokerContext
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedCommandRequest,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _seed_broker_actor_process,
)
from .test_outside_workspace_command_approval import (
    APPROVAL_MODES,
    COMMAND_TOOLS,
    PYTHON_CODE,
    _approval_rows,
    _decide,
    _run,
    _setup,
)
from .test_outside_workspace_conversation_grant import _allow_for_conversation

JUSTIFICATION = "Save the tool's settings so it can run."


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


def _args(
    tool_id: str,
    *,
    folders: list[JSONValue] | None = None,
    justification: str | None = JUSTIFICATION,
    cwd: Path | None = None,
) -> dict[str, JSONValue]:
    args: dict[str, JSONValue] = (
        {"command": "touch made.txt"}
        if tool_id == "bash"
        else {"code": PYTHON_CODE, "args": []}
    )
    if cwd is not None:
        args["cwd"] = str(cwd)
    if folders is not None:
        args["additional_write_folders"] = folders
    if justification is not None:
        args["justification"] = justification
    return args


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_requested_folder_asks_and_opens_for_the_approved_call_only(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    tool_id: str,
    mode: str,
) -> None:
    db_path, context = _setup(tmp_path, mode)
    args = _args(tool_id, folders=[str(outside)])

    # Asked even when commands are auto-approved, with the reason to show.
    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="r1",
            args=args,
        )
    [(_, status, summary)] = _approval_rows(db_path)
    assert status == "pending"
    assert summary["reason"] == JUSTIFICATION
    assert summary["outside_workspace"] == {
        "folders": [{"path": str(outside), "display_name": outside.name}],
        "can_allow_for_conversation": True,
    }
    _decide(db_path, asked.value, "r1", "approved_once")
    await _run(
        db_path=db_path, context=context, tool_id=tool_id, request_id="r1", args=args
    )

    [command] = launched
    # The command still runs in the workspace; only the write roots grow.
    assert command.cwd == str(context.workspace_path.resolve())
    assert str(outside) in command.real_write_roots
    if mode == "always_allow":
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="r2",
            args=_args(tool_id, justification=None),
        )
        assert str(outside) not in launched[-1].real_write_roots


@pytest.mark.asyncio
async def test_conversation_grant_of_a_requested_folder_stops_asking(
    tmp_path: Path, outside: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    args = _args("bash", folders=[str(outside)])
    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
        )

    _allow_for_conversation(db_path, asked.value, "r1")
    await _run(
        db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
    )
    # The same request later names a folder that is now open: no new question.
    await _run(
        db_path=db_path, context=context, tool_id="bash", request_id="r2", args=args
    )

    assert len(launched) == 2
    assert all(str(outside) in command.real_write_roots for command in launched)
    assert [row[0] for row in _approval_rows(db_path)] == ["r1", "r2"]
    assert "outside_workspace" not in _approval_rows(db_path)[1][2]


@pytest.mark.asyncio
async def test_home_relative_folder_names_the_real_home(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    monkeypatch.setenv("HOME", str(outside.parent))
    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_args("bash", folders=[f"~/{outside.name}"]),
        )

    [(_, _, summary)] = _approval_rows(db_path)
    assert summary["outside_workspace"]["folders"] == [  # type: ignore[index]
        {"path": str(outside), "display_name": outside.name}
    ]


@pytest.mark.asyncio
async def test_outside_cwd_and_the_same_requested_folder_are_asked_once(
    tmp_path: Path, outside: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_args("bash", folders=[str(outside)], cwd=outside),
        )

    [(_, _, summary)] = _approval_rows(db_path)
    assert summary["reason"] == JUSTIFICATION
    assert summary["outside_workspace"] == {
        "folders": [{"path": str(outside), "display_name": outside.name}],
        "can_allow_for_conversation": True,
    }


@pytest.mark.asyncio
async def test_folder_inside_the_workspace_runs_without_asking(
    tmp_path: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    inside = context.workspace_path / "build"
    inside.mkdir()

    await _run(
        db_path=db_path,
        context=context,
        tool_id="bash",
        request_id="r1",
        args=_args("bash", folders=[str(inside)]),
    )

    [command] = launched
    assert "outside_workspace" not in command.command_summary_json


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
async def test_protected_or_unusable_folders_are_refused_without_asking(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    (context.workspace_path / "escape").symlink_to(outside)
    artifact_root = read_local_runtime_artifact_root()
    artifact_root.mkdir(parents=True, exist_ok=True)
    # The home folder here encloses the app database folder, as it does for users.
    monkeypatch.setenv("HOME", str(tmp_path.parent))

    refused = {
        "relative path": "outside-folder",
        "missing folder": str(outside / "missing"),
        "a file": str(context.workspace_path / "file.txt"),
        "symlink escape from the workspace": str(context.workspace_path / "escape"),
        "app database folder": str(tmp_path),
        "app artifact storage": str(artifact_root),
        "the home folder": "~",
        "folder enclosing app storage": "/",
    }
    (context.workspace_path / "file.txt").write_text("x")
    for index, (label, folder) in enumerate(refused.items()):
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(
                db_path=db_path,
                context=context,
                tool_id=tool_id,
                request_id=f"r-{index}",
                args=_args(tool_id, folders=[str(outside), folder]),
            )
        assert caught.value.code == EXEC_WRITE_FOLDER_DENIED, label
        assert caught.value.fix_hint, label
        if label == "missing folder":
            assert "nearest existing parent folder" in caught.value.fix_hint
    assert _approval_rows(db_path) == []
    assert launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "folders", "justification", "login"),
    [
        ("bash", ["/tmp"], None, False),
        ("bash", None, JUSTIFICATION, False),
        ("bash", [], JUSTIFICATION, False),
        ("bash", None, None, True),
        ("run_python", ["/tmp"], None, False),
        ("run_python", None, JUSTIFICATION, False),
    ],
)
async def test_justification_comes_exactly_with_extra_access(
    tmp_path: Path,
    launched: list[ValidatedCommandRequest],
    tool_id: str,
    folders: list[JSONValue] | None,
    justification: str | None,
    login: bool,
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    args = _args(tool_id, folders=folders, justification=justification)
    if login:
        args["use_login_environment"] = True
    with pytest.raises(BrokerPolicyError) as caught:
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="r1",
            args=args,
        )

    assert caught.value.code == BROKER_TOOL_ARGS_INVALID
    assert launched == []


@pytest.mark.asyncio
async def test_login_environment_approval_carries_the_reason(
    tmp_path: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    args = _args("bash")
    args["use_login_environment"] = True
    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
        )

    [(_, _, summary)] = _approval_rows(db_path)
    assert summary["use_login_environment"] is True
    assert summary["reason"] == JUSTIFICATION
    assert "outside_workspace" not in summary


@pytest.mark.asyncio
async def test_subagent_folder_request_is_denied_without_asking(
    tmp_path: Path, outside: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    _seed_broker_actor_process(
        db_path,
        process_id="child-1",
        kind="action_subagent",
        parent_process_id=BROKER_ACTOR_PROCESS_ID,
    )
    (context.workspace_path / "claimed").mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id="user-1",
                action_id="action-1",
                parent_process_id=BROKER_ACTOR_PROCESS_ID,
                child_process_id="child-1",
                acquired_at=BROKER_ACTOR_TIMESTAMP,
                claims=(
                    WorkspacePathResourceClaim(
                        "claim-1",
                        context.manifest_id,
                        "claimed",
                        context.workspace_path,
                    ),
                ),
            )

    with pytest.raises(BrokerPolicyError) as caught:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r-child",
            args=_args(
                "bash", folders=[str(outside)], cwd=context.workspace_path / "claimed"
            ),
            actor_process_id="child-1",
        )

    assert caught.value.code == ACTION_SUBAGENT_WRITE_DENIED
    assert _approval_rows(db_path) == []
    assert launched == []
