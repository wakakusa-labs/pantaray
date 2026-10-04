from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
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
from pantaray_agents.local_runtime.tooling.brokering import broker_direct
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
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerPreflightOutcome,
    BrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import (
    load_tool_results_root,
)
from pantaray_agents.local_runtime.tooling.brokering.tool_path_policy import (
    ACTION_PLAN_PATH_PRIVATE,
    WRITE_PATH_DENIED,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
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


def _set_approval_mode(db_path: Path, mode: str) -> None:
    if mode == "always_allow":
        _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    else:
        _set_prompt_preference(db_path)


def _add_args(path: Path | str, line: str = "created") -> dict[str, JSONValue]:
    return {
        "changes": [
            {
                "op": "add",
                "path": str(path),
                "new_lines": [line],
                "trailing_newline": True,
            }
        ]
    }


async def _patch(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    request_id: str,
    args: dict[str, JSONValue],
    actor_process_id: str = BROKER_ACTOR_PROCESS_ID,
) -> BrokerPreflightOutcome | BrokerToolOutcome:
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_outside_workspace_patch_asks_in_every_approval_mode(
    tmp_path: Path, outside: Path, mode: str
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_approval_mode(db_path, mode)
    target = outside / "notes.txt"

    with pytest.raises(BrokerApprovalRequiredError):
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )

    assert _approval_rows(db_path) == [
        (
            "req-1",
            "pending",
            {
                "summary_kind": "apply_patch",
                "target_paths": [str(target)],
                "outside_workspace": {
                    "folders": [{"path": str(outside), "display_name": outside.name}],
                    "can_allow_for_conversation": True,
                },
            },
        )
    ]
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", APPROVAL_MODES)
async def test_inside_workspace_patch_keeps_its_approval_behavior(
    tmp_path: Path, mode: str
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_approval_mode(db_path, mode)

    if mode == "always_allow":
        await _patch(
            db_path=db_path,
            context=context,
            request_id="req-in",
            args=_add_args("a.txt"),
        )
        assert (context.workspace_path / "a.txt").is_file()
        expected_status = "approved_once"
    else:
        with pytest.raises(BrokerApprovalRequiredError):
            await _patch(
                db_path=db_path,
                context=context,
                request_id="req-in",
                args=_add_args("a.txt"),
            )
        expected_status = "pending"
    assert _approval_rows(db_path) == [
        (
            "req-in",
            expected_status,
            {"summary_kind": "apply_patch", "target_paths": ["a.txt"]},
        )
    ]


@pytest.mark.asyncio
async def test_approve_once_writes_only_that_call(
    tmp_path: Path, outside: Path
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    target = outside / "notes.txt"

    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )
    _decide(db_path, first.value, "req-1", "approved_once")
    outcome = await _patch(
        db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
    )

    assert outcome.status == "success"
    assert isinstance(outcome.output, dict)
    assert outcome.output["applied_paths"] == [str(target)]
    assert target.read_text(encoding="utf-8") == "created\n"

    second_target = outside / "second.txt"
    with pytest.raises(BrokerApprovalRequiredError):
        await _patch(
            db_path=db_path,
            context=context,
            request_id="req-2",
            args=_add_args(second_target),
        )
    assert not second_target.exists()
    assert [(row[0], row[1]) for row in _approval_rows(db_path)] == [
        ("req-1", "approved_once"),
        ("req-2", "pending"),
    ]


@pytest.mark.asyncio
async def test_denied_outside_workspace_patch_is_not_written(
    tmp_path: Path, outside: Path
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    target = outside / "notes.txt"

    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )
    _decide(db_path, first.value, "req-1", "denied")
    with pytest.raises(BrokerApprovalDeniedError):
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )
    assert not target.exists()


@pytest.mark.asyncio
async def test_approved_folder_is_rechecked_before_the_write(
    tmp_path: Path,
    outside: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    target = outside / "notes.txt"
    elsewhere = tmp_path_factory.mktemp("elsewhere").resolve()
    with pytest.raises(BrokerApprovalRequiredError) as first:
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )
    _decide(db_path, first.value, "req-1", "approved_once")
    real_plan = broker_direct.resolve_patch_mount_plan

    def retargeted_plan(**kwargs: object) -> broker_direct.PatchMountPlan:
        plan = real_plan(**kwargs)  # type: ignore[arg-type]
        return replace(plan, outside_workspace_folder=elsewhere)

    # Only the executor's re-resolution sees the moved folder.
    monkeypatch.setattr(broker_direct, "resolve_patch_mount_plan", retargeted_plan)
    with pytest.raises(BrokerPolicyError) as caught:
        await _patch(
            db_path=db_path, context=context, request_id="req-1", args=_add_args(target)
        )
    assert caught.value.code == broker_direct.PATCH_ERROR_PATH_RETARGETED
    assert not target.exists()


@pytest.mark.asyncio
async def test_protected_paths_stay_hard_denied(tmp_path: Path, outside: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    (context.workspace_path / "escape").symlink_to(outside)
    tool_results = load_tool_results_root(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        manifest_id=context.manifest_id,
        action_id="action-1",
    )
    (outside / "results-link").symlink_to(tool_results)
    plan = context.workspace_path / "plan.md"
    plan.write_text("private\n", encoding="utf-8")
    (outside / "plan-link.md").symlink_to(plan)
    memory_users = read_local_runtime_artifact_root() / "memory_catalog/users"
    memory_users.mkdir(parents=True, exist_ok=True)

    denied_paths = {
        "symlink escape from the workspace": "escape/notes.txt",
        "tool results through an outside link": str(outside / "results-link/x.txt"),
        "memory storage": str(memory_users / "notes.txt"),
        "app database folder": str(db_path.parent / "notes.txt"),
        "missing parent folder": str(outside / "missing/notes.txt"),
    }
    for index, (label, path) in enumerate(denied_paths.items()):
        with pytest.raises(BrokerPolicyError) as caught:
            await _patch(
                db_path=db_path,
                context=context,
                request_id=f"req-denied-{index}",
                args=_add_args(path),
            )
        assert caught.value.code == WRITE_PATH_DENIED, label

    with pytest.raises(BrokerPolicyError) as plan_caught:
        await _patch(
            db_path=db_path,
            context=context,
            request_id="req-plan",
            args={
                "changes": [
                    {
                        "op": "update",
                        "path": str(outside / "plan-link.md"),
                        "edits": [{"old_lines": ["private"], "new_lines": ["x"]}],
                    }
                ]
            },
        )
    assert plan_caught.value.code == ACTION_PLAN_PATH_PRIVATE
    assert _approval_rows(db_path) == []
    assert plan.read_text(encoding="utf-8") == "private\n"
    assert set(outside.iterdir()) == {
        outside / "results-link",
        outside / "plan-link.md",
    }


@pytest.mark.asyncio
async def test_subagent_outside_workspace_patch_is_denied_without_asking(
    tmp_path: Path, outside: Path
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
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
        await _patch(
            db_path=db_path,
            context=context,
            request_id="req-child",
            args=_add_args(outside / "notes.txt"),
            actor_process_id="child-1",
        )
    assert caught.value.code == ACTION_SUBAGENT_WRITE_DENIED
    assert _approval_rows(db_path) == []


@pytest.mark.asyncio
async def test_approved_update_of_outside_file_goes_through_the_read_gate(
    tmp_path: Path, outside: Path
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    target = outside / "notes.txt"
    target.write_text("old line\n", encoding="utf-8")
    args: dict[str, JSONValue] = {
        "changes": [
            {
                "op": "update",
                "path": str(target),
                "edits": [{"old_lines": ["old line"], "new_lines": ["new line"]}],
            }
        ]
    }

    outputs: list[JSONValue] = []
    for request_id in ("req-read", "req-write"):
        with pytest.raises(BrokerApprovalRequiredError) as asked:
            await _patch(
                db_path=db_path, context=context, request_id=request_id, args=args
            )
        _decide(db_path, asked.value, request_id, "approved_once")
        outcome = await _patch(
            db_path=db_path, context=context, request_id=request_id, args=args
        )
        assert isinstance(outcome.output, dict)
        outputs.append(outcome.output["status"])

    assert outputs == ["needs_read", "success"]
    assert target.read_text(encoding="utf-8") == "new line\n"
