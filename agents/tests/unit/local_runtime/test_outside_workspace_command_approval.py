from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    WorkspacePathResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.local_runtime.tooling.brokering import (
    broker as broker_module,
)
from pantaray_agents.local_runtime.tooling.brokering import (
    broker_command_validation,
)
from pantaray_agents.local_runtime.tooling.brokering.action_subagent_broker_authority import (
    ACTION_SUBAGENT_WRITE_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerPolicyError,
    apply_approval_decision,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerContext,
    load_broker_context,
)
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
from pantaray_agents.local_runtime.tooling.brokering.outside_workspace import (
    OutsideWorkspaceCwd,
)
from pantaray_agents.local_runtime.tooling.brokering.tool_path_policy import (
    EXEC_CWD_DENIED,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.local_runtime.tooling.sandbox.runtime_policy import (
    PROFILE_TIMEOUT_MS,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
    _seed_broker_actor_process,
)
from .test_tool_broker_approval import _set_prompt_preference

APPROVAL_MODES = ("prompt_each_time", "always_allow")
COMMAND_TOOLS = ("bash", "run_python")
PYTHON_CODE = "open('made.txt', 'w').write('x')"


def _setup(tmp_path: Path, mode: str) -> tuple[Path, ActionExecutionContext]:
    db_path, context = _bootstrap_runtime_db(
        tmp_path, allowed_tool_ids=("read", "apply_patch", "bash", "run_python")
    )
    if mode == "always_allow":
        _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    else:
        _set_prompt_preference(db_path)
    assert isinstance(context, ActionExecutionContext)
    return db_path, context


def _args(tool_id: str, cwd: Path | str) -> dict[str, JSONValue]:
    if tool_id == "bash":
        return {"command": "touch made.txt", "cwd": str(cwd)}
    return {"code": PYTHON_CODE, "args": [], "cwd": str(cwd)}


def _expected_summary(tool_id: str, folder: Path) -> dict[str, JSONValue]:
    outside_workspace: dict[str, JSONValue] = {
        "folders": [{"path": str(folder), "display_name": folder.name}],
        "can_allow_for_conversation": True,
    }
    if tool_id == "bash":
        return {
            "summary_kind": "bash",
            "command": "touch made.txt",
            "cwd": str(folder),
            "timeout_ms": PROFILE_TIMEOUT_MS["workspace_process_exec"],
            "use_login_environment": False,
            "reason": None,
            "outside_workspace": outside_workspace,
        }
    code = PYTHON_CODE.encode("utf-8")
    return {
        "summary_kind": "run_python",
        "cwd": str(folder),
        "code_sha256": sha256(code).hexdigest(),
        "code_size_bytes": len(code),
        "args_count": 0,
        "timeout_ms": PROFILE_TIMEOUT_MS["agent_generated_python"],
        "reason": None,
        "outside_workspace": outside_workspace,
    }


async def _run(
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


def _approval_rows(db_path: Path) -> list[tuple[str, str, dict[str, JSONValue]]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT tool_request_id, status, command_summary_json "
            "FROM approval_sessions ORDER BY requested_at, tool_request_id"
        ).fetchall()
    return [(row[0], row[1], json.loads(row[2])) for row in rows]


def _decide(
    db_path: Path,
    error: BrokerApprovalRequiredError,
    request_id: str,
    decision: Literal["approved_once", "denied"],
) -> None:
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id=request_id,
        user_id="user-1",
        action_id="action-1",
        approval_session_id=error.approval_session_id,
        decision=decision,
        decided_at=BROKER_ACTOR_TIMESTAMP,
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_outside_workspace_cwd_asks_in_every_approval_mode(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    mode: str,
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, mode)

    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-1",
            args=_args(tool_id, outside),
        )

    assert _approval_rows(db_path) == [
        ("req-1", "pending", _expected_summary(tool_id, outside))
    ]
    assert launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
async def test_approve_once_opens_the_folder_for_that_call_only(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    args = _args(tool_id, outside)

    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-1",
            args=args,
        )
    _decide(db_path, first.value, "req-1", "approved_once")
    outcome = await _run(
        db_path=db_path, context=context, tool_id=tool_id, request_id="req-1", args=args
    )

    assert outcome.status == "success"
    [request] = launched
    assert request.cwd == str(outside)
    assert str(outside) in request.real_read_roots
    assert str(outside) in request.real_write_roots
    # The sandbox's app-storage protection is unchanged by the approval.
    assert str(read_local_runtime_artifact_root().resolve()) in (
        request.private_storage_roots
    )

    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-2",
            args=args,
        )
    assert len(launched) == 1
    assert [(row[0], row[1]) for row in _approval_rows(db_path)] == [
        ("req-1", "approved_once"),
        ("req-2", "pending"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
async def test_denied_outside_workspace_command_does_not_run(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    args = _args(tool_id, outside)

    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-1",
            args=args,
        )
    _decide(db_path, first.value, "req-1", "denied")
    with pytest.raises(BrokerApprovalDeniedError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-1",
            args=args,
        )
    assert launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_inside_workspace_command_keeps_its_approval_behavior(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    mode: str,
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, mode)
    args = _args(tool_id, ".")

    if mode == "always_allow":
        await _run(
            db_path=db_path,
            context=context,
            tool_id=tool_id,
            request_id="req-in",
            args=args,
        )
        [request] = launched
        assert str(outside) not in request.real_write_roots
        expected_status = "approved_once"
    else:
        with pytest.raises(BrokerApprovalRequiredError):
            await _run(
                db_path=db_path,
                context=context,
                tool_id=tool_id,
                request_id="req-in",
                args=args,
            )
        expected_status = "pending"
    [(request_id, status, summary)] = _approval_rows(db_path)
    assert (request_id, status) == ("req-in", expected_status)
    assert "outside_workspace" not in summary
    assert summary["cwd"] == str(context.workspace_path.resolve())


@pytest.mark.asyncio
async def test_approved_cwd_is_rechecked_before_launch(
    tmp_path: Path,
    outside: Path,
    tmp_path_factory: pytest.TempPathFactory,
    launched: list[ValidatedCommandRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    args = _args("bash", outside)
    elsewhere = tmp_path_factory.mktemp("elsewhere").resolve()
    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-1",
            args=args,
        )
    _decide(db_path, first.value, "req-1", "approved_once")
    real_resolve = broker_command_validation.resolve_exec_tool_cwd
    calls: list[str | None] = []

    def moved_after_validation(
        *, context: BrokerContext, raw_cwd: str | None
    ) -> object:
        calls.append(raw_cwd)
        if len(calls) == 1:
            return real_resolve(context=context, raw_cwd=raw_cwd)
        return OutsideWorkspaceCwd(path=elsewhere)

    # Only the pre-launch re-resolution sees the moved folder.
    monkeypatch.setattr(
        broker_command_validation, "resolve_exec_tool_cwd", moved_after_validation
    )
    with pytest.raises(BrokerPolicyError) as caught:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-1",
            args=args,
        )
    assert caught.value.code == broker_command_validation.EXEC_CWD_RETARGETED
    assert len(calls) == 2
    assert launched == []


def test_every_approved_folder_is_rechecked_before_launch(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    broker_context = load_broker_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        path_access_kind="exec",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
    )
    first = tmp_path_factory.mktemp("first").resolve()
    second = tmp_path_factory.mktemp("second").resolve()
    elsewhere = tmp_path_factory.mktemp("elsewhere").resolve()
    request = ValidatedCommandRequest.model_construct(
        command_summary_json=build_bash_summary(
            command="touch made.txt",
            cwd_relative_path=str(first),
            timeout_ms=1_000,
            use_login_environment=False,
            run_outside_sandbox=False,
            reason=None,
            outside_workspace_folders=(first, second),
        )
    )
    broker_command_validation.verify_outside_workspace_folders_unchanged(
        context=broker_context, request=request
    )

    # The second folder, not the cwd, is swapped for a link after approval.
    second.rmdir()
    second.symlink_to(elsewhere)
    with pytest.raises(BrokerPolicyError) as caught:
        broker_command_validation.verify_outside_workspace_folders_unchanged(
            context=broker_context, request=request
        )
    assert caught.value.code == broker_command_validation.EXEC_CWD_RETARGETED


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", COMMAND_TOOLS)
async def test_protected_cwds_stay_hard_denied(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    tool_id: str,
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    (context.workspace_path / "escape").symlink_to(outside)
    artifact_root = read_local_runtime_artifact_root()
    artifact_root.mkdir(parents=True, exist_ok=True)

    denied_cwds = {
        "symlink escape from the workspace": "escape",
        "app database folder": str(tmp_path),
        "app artifact storage": str(artifact_root),
        "folder enclosing app storage": "/",
        "missing folder": str(outside / "missing"),
    }
    for index, (label, cwd) in enumerate(denied_cwds.items()):
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(
                db_path=db_path,
                context=context,
                tool_id=tool_id,
                request_id=f"req-denied-{index}",
                args=_args(tool_id, cwd),
            )
        assert caught.value.code == EXEC_CWD_DENIED, label
    assert _approval_rows(db_path) == []
    assert launched == []


@pytest.mark.asyncio
async def test_subagent_outside_workspace_cwd_is_denied_without_asking(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
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
            request_id="req-child",
            args=_args("bash", outside),
            actor_process_id="child-1",
        )
    assert caught.value.code == ACTION_SUBAGENT_WRITE_DENIED
    assert _approval_rows(db_path) == []
    assert launched == []
