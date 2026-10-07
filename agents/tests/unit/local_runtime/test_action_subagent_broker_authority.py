from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    WorkspacePathResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.local_runtime.tooling.brokering import (
    broker_direct as broker_direct_module,
)
from pantaray_agents.local_runtime.tooling.brokering.action_subagent_broker_authority import (
    ACTION_SUBAGENT_WRITE_DENIED,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    FinalizedBrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import BrokerContext
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedPatchRequest,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from .action_seed import insert_agent_action
from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _bootstrap_runtime_db,
    _bootstrap_runtime_db_with_registered_folder,
    _grant_workspace_full_access,
    _seed_broker_actor_process,
)

BUSY_TIMEOUT_MS = 1_000


def _acquire_claim(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    child_process_id: str,
    claim_id: str,
    raw_path: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id="user-1",
                action_id="action-1",
                parent_process_id=BROKER_ACTOR_PROCESS_ID,
                child_process_id=child_process_id,
                acquired_at=BROKER_ACTOR_TIMESTAMP,
                claims=(
                    WorkspacePathResourceClaim(
                        claim_id,
                        context.manifest_id,
                        raw_path,
                        context.workspace_path,
                    ),
                ),
            )


def _add_args(path: str) -> dict[str, JSONValue]:
    return {
        "changes": [
            {
                "op": "add",
                "path": path,
                "new_lines": ["created"],
                "trailing_newline": True,
            }
        ]
    }


async def _execute_patch(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    actor_process_id: str,
    path: str,
    preflight_only: bool = False,
) -> object:
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=actor_process_id,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args=_add_args(path),
        tool_request_id=path.replace("/", "-"),
        requested_at=BROKER_ACTOR_TIMESTAMP,
        preflight_only=preflight_only,
    )


async def _assert_denied_before_audit(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    actor_process_id: str,
    path: str,
) -> None:
    with pytest.raises(BrokerPolicyError) as caught:
        await _execute_patch(
            db_path=db_path,
            context=context,
            actor_process_id=actor_process_id,
            path=path,
        )
    assert caught.value.code == ACTION_SUBAGENT_WRITE_DENIED
    with sqlite3.connect(db_path) as connection:
        invocation = connection.execute(
            "SELECT 1 FROM tool_invocations WHERE tool_request_id = ?",
            (path.replace("/", "-"),),
        ).fetchone()
    assert invocation is None
    assert not (context.workspace_path / path).exists()


@pytest.mark.asyncio
async def test_broker_write_authority_follows_actor_and_active_claims(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    (context.workspace_path / "claimed").mkdir()
    for process_id, status in (
        ("child-1", "running"),
        ("child-no-claim", "running"),
        ("terminal-child", "completed"),
    ):
        _seed_broker_actor_process(
            db_path,
            process_id=process_id,
            kind="action_subagent",
            status=status,
            parent_process_id=BROKER_ACTOR_PROCESS_ID,
        )
    insert_agent_action(
        db_path=db_path,
        suggestion_id="suggestion-other",
        action_id="other-action",
    )
    _seed_broker_actor_process(
        db_path,
        process_id="other-parent",
        action_id="other-action",
    )
    _acquire_claim(
        db_path=db_path,
        context=context,
        child_process_id="child-1",
        claim_id="claim-1",
        raw_path="claimed",
    )

    denied = (
        (BROKER_ACTOR_PROCESS_ID, "claimed/parent.txt"),
        ("child-1", "outside-child.txt"),
        ("child-no-claim", "claimed/no-claim.txt"),
        ("terminal-child", "terminal.txt"),
        ("other-parent", "cross-lineage.txt"),
    )
    for actor_process_id, path in denied:
        await _assert_denied_before_audit(
            db_path=db_path,
            context=context,
            actor_process_id=actor_process_id,
            path=path,
        )

    await _execute_patch(
        db_path=db_path,
        context=context,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        path="parent-outside.txt",
    )
    await _execute_patch(
        db_path=db_path,
        context=context,
        actor_process_id="child-1",
        path="claimed/nested/child.txt",
    )
    assert (context.workspace_path / "parent-outside.txt").is_file()
    assert (context.workspace_path / "claimed/nested/child.txt").is_file()

    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                "UPDATE action_subagent_resource_claims SET released_at = ? "
                "WHERE claim_id = 'claim-1'",
                ("2026-09-01T00:02:00Z",),
            )
    await _assert_denied_before_audit(
        db_path=db_path,
        context=context,
        actor_process_id="child-1",
        path="claimed/released.txt",
    )


@pytest.mark.asyncio
async def test_apply_patch_rechecks_claims_immediately_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    _seed_broker_actor_process(
        db_path,
        process_id="child-race",
        kind="action_subagent",
        parent_process_id=BROKER_ACTOR_PROCESS_ID,
    )
    await _execute_patch(
        db_path=db_path,
        context=context,
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        path="race.txt",
        preflight_only=True,
    )

    action_context = context
    original_needs_read = broker_direct_module._needs_read_output_if_required

    def needs_read_then_resource(
        *,
        context: BrokerContext,
        patch_root: Path,
        path_rewrites: dict[str, str],
        request: ValidatedPatchRequest,
    ) -> dict[str, JSONValue] | None:
        needs_read = original_needs_read(
            context=context,
            patch_root=patch_root,
            path_rewrites=path_rewrites,
            request=request,
        )
        _acquire_claim(
            db_path=db_path,
            context=action_context,
            child_process_id="child-race",
            claim_id="claim-race",
            raw_path="race.txt",
        )
        return needs_read

    monkeypatch.setattr(
        broker_direct_module,
        "_needs_read_output_if_required",
        needs_read_then_resource,
    )
    with pytest.raises(FinalizedBrokerPolicyError) as caught:
        await _execute_patch(
            db_path=db_path,
            context=context,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            path="race.txt",
        )
    assert caught.value.cause.code == ACTION_SUBAGENT_WRITE_DENIED
    assert not (context.workspace_path / "race.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_rejects_retargeted_locked_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context, repo, _folder = _bootstrap_runtime_db_with_registered_folder(
        tmp_path
    )
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    parent = repo / "parent"
    parent.mkdir()
    original_parent = repo / "original-parent"
    retarget = context.workspace_path / "retarget"
    retarget.mkdir()
    alias = context.workspace_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)

    original_needs_read = broker_direct_module._needs_read_output_if_required

    def retarget_after_locked_read(
        *,
        context: BrokerContext,
        patch_root: Path,
        path_rewrites: dict[str, str],
        request: ValidatedPatchRequest,
    ) -> dict[str, JSONValue] | None:
        needs_read = original_needs_read(
            context=context,
            patch_root=patch_root,
            path_rewrites=path_rewrites,
            request=request,
        )
        parent.rename(original_parent)
        parent.symlink_to(retarget, target_is_directory=True)
        return needs_read

    monkeypatch.setattr(
        broker_direct_module,
        "_needs_read_output_if_required",
        retarget_after_locked_read,
    )
    with pytest.raises(FinalizedBrokerPolicyError) as caught:
        await _execute_patch(
            db_path=db_path,
            context=context,
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            path="alias/file.txt",
        )
    assert caught.value.cause.code == broker_direct_module.PATCH_ERROR_PATH_RETARGETED
    assert not (original_parent / "file.txt").exists()
    assert not (retarget / "file.txt").exists()
