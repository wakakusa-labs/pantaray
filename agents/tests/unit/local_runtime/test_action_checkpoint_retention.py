from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.local_runtime.runtime.action_message_process_fence import (
    ActionLogicalRunLineage,
)
from pantaray_agents.local_runtime.runtime.action_resumability import (
    action_latest_run_has_restorable_checkpoint,
)
from pantaray_agents.local_runtime.runtime.action_startup_recovery_envelope import (
    _load_anchor,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.base import JSONValue, StepStatusType
from pantaray_agents.tasks.action_user_message import serialize_action_user_message

from .local_action_repository_support import (
    ACTION_ID,
    USER_ID,
    bootstrap_action_repository_db,
    build_action_repository,
    save_processing_action,
)

APPROVAL = {"approval_session_id": "approval-1", "tool_request_id": "request-1"}
PAUSED_CHECKPOINT: dict[str, JSONValue] = {
    "step": 3,
    "pending_approval_request": dict(APPROVAL),
    "current_approval_blockers": [dict(APPROVAL)],
}


async def _repository(tmp_path: Path) -> tuple[Path, LocalActionRepository]:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_processing_action(repo)
    _insert_user_step(db_path, step_id="user-1", step_number=1)
    return db_path, repo


def _insert_user_step(db_path: Path, *, step_id: str, step_number: int) -> None:
    message = ActionUserMessageInput(message_id=f"message-{step_id}", content="go")
    process_id = f"process-{step_id}"
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO processes(process_id,user_id,kind,status,action_id,
                started_at,updated_at,completed_at,heartbeat_at,next_event_seq)
            VALUES (?,?,'action','completed',?,'now','now','now','now',1)""",
            (process_id, USER_ID, ACTION_ID),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,accepted_sequence,
                local_step_number,short_step_id,step_type,step_name,status,goal_handle,
                retry_count,prompt_tokens,completion_tokens,user_message_id,
                user_message_json,user_request_text,adopted_process_id,created_at
            ) VALUES (?,?,?,?,?,?,?,'user_request','user_request','success','S',0,0,0,
                      ?,?,'go',?,'2026-10-01T00:00:00Z')""",
            (
                step_id,
                ACTION_ID,
                USER_ID,
                step_number,
                step_number,
                step_number,
                f"S-{step_number}-USER",
                message.message_id,
                serialize_action_user_message(message),
                process_id,
            ),
        )


async def _save_step(
    repo: LocalActionRepository,
    *,
    step_id: str,
    step_number: int,
    checkpoint: dict[str, JSONValue],
    step_type: StepType = StepType.LLM_OUTPUT,
    status: StepStatusType = StepStatusType.SUCCESS,
) -> None:
    result = await repo.save_action_step(
        step_id=step_id,
        action_id=ACTION_ID,
        step_number=step_number,
        step_name=(
            "supervisor_think" if step_type == StepType.LLM_OUTPUT else "tool::shell"
        ),
        step_type=step_type,
        runtime_state_checkpoint=checkpoint,
        runtime_state_checkpoint_version=RUNTIME_STATE_CHECKPOINT_VERSION,
        status=status,
        started_at=f"2026-10-01T00:{step_number:02d}:00Z",
        completed_at=f"2026-10-01T00:{step_number:02d}:30Z",
        goal_handle="S",
        user_id=USER_ID,
        short_step_id=(
            f"S-{step_number}-THINK"
            if step_type == StepType.LLM_OUTPUT
            else f"S-{step_number}-TOOL"
        ),
        local_step_number=step_number,
    )
    assert result.error is None


def _checkpoint_rows(db_path: Path) -> dict[str, JSONValue]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT step_id, runtime_state_checkpoint FROM agent_action_steps
               WHERE runtime_state_checkpoint IS NOT NULL"""
        ).fetchall()
    return {step_id: json.loads(raw) for step_id, raw in rows}


@pytest.mark.asyncio
async def test_each_checkpoint_supersedes_the_previous_ones(tmp_path: Path) -> None:
    db_path, repo = await _repository(tmp_path)
    for step_number in range(2, 7):
        await _save_step(
            repo,
            step_id=f"step-{step_number}",
            step_number=step_number,
            checkpoint={"step": step_number + 1},
        )

    assert _checkpoint_rows(db_path) == {"step-6": {"step": 7}}
    resume = await repo.get_runtime_resume_context_for_user_step(
        user_id=USER_ID, action_id=ACTION_ID, current_user_step_number=7
    )
    assert resume.data is not None
    assert resume.data.checkpoint_row is not None
    assert resume.data.checkpoint_row["step_id"] == "step-6"
    with sqlite3.connect(db_path) as connection:
        assert action_latest_run_has_restorable_checkpoint(
            connection=connection, user_id=USER_ID, action_id=ACTION_ID
        )


@pytest.mark.asyncio
async def test_follow_up_turn_resumes_from_the_previous_run_checkpoint(
    tmp_path: Path,
) -> None:
    db_path, repo = await _repository(tmp_path)
    await _save_step(repo, step_id="step-2", step_number=2, checkpoint={"step": 3})
    await _save_step(repo, step_id="step-3", step_number=3, checkpoint={"step": 4})
    _insert_user_step(db_path, step_id="user-4", step_number=4)

    resume = await repo.get_runtime_resume_context_for_user_step(
        user_id=USER_ID, action_id=ACTION_ID, current_user_step_number=4
    )

    assert resume.data is not None
    assert resume.data.checkpoint_row is not None
    assert resume.data.checkpoint_row["step_id"] == "step-3"
    assert resume.data.checkpoint_row["runtime_state_checkpoint"] == {"step": 4}
    assert resume.data.intervening_user_step is None

    await _save_step(repo, step_id="step-5", step_number=5, checkpoint={"step": 6})
    assert _checkpoint_rows(db_path) == {"step-5": {"step": 6}}


@pytest.mark.asyncio
async def test_unsettled_approval_pause_stays_readable_after_later_steps(
    tmp_path: Path,
) -> None:
    # A continuation whose approval was already claimed does not re-persist the
    # paused row, so later checkpoints follow while the pause row still names it.
    db_path, repo = await _repository(tmp_path)
    await _save_step(repo, step_id="step-2", step_number=2, checkpoint={"step": 3})
    await _save_step(
        repo,
        step_id="paused-tool",
        step_number=3,
        checkpoint=PAUSED_CHECKPOINT,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatusType.PROCESSING,
    )
    await _save_step(repo, step_id="step-4", step_number=4, checkpoint={"step": 5})
    await _save_step(repo, step_id="step-5", step_number=5, checkpoint={"step": 6})

    assert _checkpoint_rows(db_path) == {
        "paused-tool": PAUSED_CHECKPOINT,
        "step-5": {"step": 6},
    }
    continuation = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id=USER_ID, action_id=ACTION_ID, **APPROVAL
    )
    assert continuation.data is not None
    assert continuation.data["step_id"] == "step-5"
    assert continuation.metadata == {"approval_anchor_step_id": "paused-tool"}
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        anchor = _load_anchor(
            connection=connection,
            user_id=USER_ID,
            action_id=ACTION_ID,
            continuation={"kind": "tool_approval", **APPROVAL},
            lineage=ActionLogicalRunLineage(
                root_process_id="unused", root_accepted_sequence=1, job_id="unused"
            ),
        )
    assert anchor.step_id == "paused-tool"


@pytest.mark.asyncio
async def test_settled_approval_pause_is_superseded_like_any_step(
    tmp_path: Path,
) -> None:
    db_path, repo = await _repository(tmp_path)
    await _save_step(
        repo,
        step_id="paused-tool",
        step_number=2,
        checkpoint=PAUSED_CHECKPOINT,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatusType.PROCESSING,
    )
    continuation = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id=USER_ID, action_id=ACTION_ID, **APPROVAL
    )
    assert continuation.data is not None
    assert continuation.data["step_id"] == "paused-tool"

    # Resuming settles the paused row in place without the approval.
    await _save_step(
        repo,
        step_id="paused-tool",
        step_number=2,
        checkpoint={"step": 3},
        step_type=StepType.TOOL_EXECUTION,
    )
    await _save_step(repo, step_id="step-3", step_number=3, checkpoint={"step": 4})

    assert _checkpoint_rows(db_path) == {"step-3": {"step": 4}}
