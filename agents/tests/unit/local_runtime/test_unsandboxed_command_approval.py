from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.brokering.action_subagent_broker_authority import (
    ACTION_SUBAGENT_UNSANDBOXED_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolArgs,
    ValidatedCommandRequest,
)
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    OutsideWorkspaceGrantError,
    grant_outside_workspace_folder_in_connection,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    build_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_worker import (
    _launch_argv,
)
from pantaray_agents.local_runtime.tooling.sandbox.runtime_policy import (
    PROFILE_TIMEOUT_MS,
)
from pantaray_agents.schema.agent.base import JSONValue

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _seed_broker_actor_process,
)
from .test_outside_workspace_command_approval import (
    APPROVAL_MODES,
    _approval_rows,
    _decide,
    _run,
    _setup,
    launched,
    outside,
)
from .test_seatbelt_profiles import _validated_command_request

__all__ = ["launched", "outside"]

REASON = "ブラウザで資料のページを開き、PDF に書き出します。"


def _unsandboxed(
    command: str = "print-page", **extra: JSONValue
) -> dict[str, JSONValue]:
    return {
        "command": command,
        "run_outside_sandbox": True,
        "justification": REASON,
        **extra,
    }


@pytest.mark.parametrize(
    "args",
    [
        {"command": "print-page", "run_outside_sandbox": True},
        {
            "command": "print-page",
            "run_outside_sandbox": True,
            "justification": REASON,
            "additional_write_folders": ["/Users/me/out"],
        },
    ],
    ids=["without justification", "with write folders"],
)
def test_run_outside_sandbox_needs_a_reason_and_no_write_folders(
    args: dict[str, JSONValue],
) -> None:
    with pytest.raises(ValidationError):
        BashToolArgs.model_validate(args)


def test_run_outside_sandbox_with_a_reason_is_accepted() -> None:
    parsed = BashToolArgs.model_validate(_unsandboxed())

    assert parsed.run_outside_sandbox is True
    assert parsed.justification == REASON


@pytest.mark.parametrize("run_outside_sandbox", [False, True])
def test_only_an_unsandboxed_request_starts_without_the_profile(
    tmp_path: Path, run_outside_sandbox: bool
) -> None:
    request = _validated_command_request(network_policy="deny")
    sandbox_request = build_sandbox_request(
        request=request.model_copy(update={"run_outside_sandbox": run_outside_sandbox}),
        temp_dir=tmp_path,
    )
    argv = _launch_argv(sandbox_request)

    if run_outside_sandbox:
        assert argv == request.argv
        assert not (tmp_path / "command.sb").exists()
    else:
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-f"]
        assert argv[3:] == request.argv


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_unsandboxed_command_asks_in_every_approval_mode(
    tmp_path: Path,
    outside: Path,
    launched: list[ValidatedCommandRequest],
    mode: str,
) -> None:
    db_path, context = _setup(tmp_path, mode)

    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-1",
            # An outside cwd names no folder: the run is not confined to one.
            args=_unsandboxed(cwd=str(outside)),
        )

    assert _approval_rows(db_path) == [
        (
            "req-1",
            "pending",
            {
                "summary_kind": "bash",
                "command": "print-page",
                "cwd": str(outside),
                "timeout_ms": PROFILE_TIMEOUT_MS["workspace_process_exec"],
                "use_login_environment": False,
                "reason": REASON,
                "run_outside_sandbox": True,
            },
        )
    ]
    assert launched == []


@pytest.mark.asyncio
async def test_approval_runs_that_one_command_unsandboxed_once(
    tmp_path: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")
    args = _unsandboxed()

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
        )
    _decide(db_path, asked.value, "r1", "approved_once")
    outcome = await _run(
        db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
    )

    assert outcome.status == "success"
    [request] = launched
    assert request.run_outside_sandbox is True
    assert request.argv[-1] == "print-page"
    assert request.approval_source == "prompt"

    # The approval was claimed by that run; the same request cannot run again.
    with pytest.raises(BrokerPolicyError, match="already been claimed"):
        await _run(
            db_path=db_path, context=context, tool_id="bash", request_id="r1", args=args
        )
    # The same command in a later call asks again: nothing is remembered.
    with pytest.raises(BrokerApprovalRequiredError):
        await _run(
            db_path=db_path, context=context, tool_id="bash", request_id="r2", args=args
        )
    assert len(launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        _unsandboxed("print-page; cat ~/.ssh/id_ed25519"),
        {"command": "print-page"},
    ],
    ids=["other command", "same command inside the sandbox"],
)
async def test_an_approval_covers_only_the_exact_unsandboxed_command(
    tmp_path: Path,
    launched: list[ValidatedCommandRequest],
    changed: dict[str, JSONValue],
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_unsandboxed(),
        )
    _decide(db_path, asked.value, "r1", "approved_once")
    with pytest.raises(BrokerPolicyError, match="does not match"):
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=changed,
        )
    assert launched == []


@pytest.mark.asyncio
async def test_a_denied_unsandboxed_command_does_not_run(
    tmp_path: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "always_allow")

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_unsandboxed(),
        )
    _decide(db_path, asked.value, "r1", "denied")
    with pytest.raises(BrokerApprovalDeniedError):
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_unsandboxed(),
        )
    assert launched == []


@pytest.mark.asyncio
async def test_an_unsandboxed_approval_cannot_be_allowed_for_the_conversation(
    tmp_path: Path, outside: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="r1",
            args=_unsandboxed(cwd=str(outside)),
        )
    with sqlite3.connect(db_path) as connection:
        with immediate_transaction(connection):
            with pytest.raises(OutsideWorkspaceGrantError):
                grant_outside_workspace_folder_in_connection(
                    connection,
                    user_id="user-1",
                    action_id="action-1",
                    approval_session_id=asked.value.approval_session_id,
                    db_path=db_path,
                    granted_at=BROKER_ACTOR_TIMESTAMP,
                )


@pytest.mark.asyncio
async def test_a_subagent_cannot_ask_to_run_outside_the_sandbox(
    tmp_path: Path, launched: list[ValidatedCommandRequest]
) -> None:
    db_path, context = _setup(tmp_path, "prompt_each_time")
    _seed_broker_actor_process(
        db_path,
        process_id="child-1",
        kind="action_subagent",
        parent_process_id=BROKER_ACTOR_PROCESS_ID,
    )

    with pytest.raises(BrokerPolicyError) as caught:
        await _run(
            db_path=db_path,
            context=context,
            tool_id="bash",
            request_id="req-child",
            args=_unsandboxed(),
            actor_process_id="child-1",
        )
    assert caught.value.code == ACTION_SUBAGENT_UNSANDBOXED_DENIED
    assert _approval_rows(db_path) == []
    assert launched == []
