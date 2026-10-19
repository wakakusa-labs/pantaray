from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import StepStatusType
from pantaray_agents.schema.repositories.repository import RepositoryErrorKind

from .local_action_repository_support import (
    ACTION_ID,
    USER_ID,
    bootstrap_action_repository_db,
    build_action_repository,
    insert_action_step,
    save_success_action,
)


@pytest.mark.asyncio
async def test_save_action_step_round_trip(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)

    denied = await repo.save_action_step(
        step_id="step-1",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-1-THINK",
        local_step_number=1,
    )
    assert denied.error == "save_action_step: user_id is required"
    assert denied.error_kind is RepositoryErrorKind.VALIDATION
    assert denied.retryable is False

    saved = await repo.save_action_step(
        step_id="step-1",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        llm_prompt_text="prompt",
        llm_response_text="response",
        tool_args={"x": 1},
        runtime_state_checkpoint={"step": 1},
        runtime_state_checkpoint_version=1,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-1-THINK",
        local_step_number=1,
        user_id=USER_ID,
    )

    assert saved.error is None
    state_result = await repo.get_action_steps_by_short_step_ids(
        user_id=USER_ID,
        action_id=ACTION_ID,
        short_step_ids=("G1-1-THINK",),
    )

    assert state_result.error is None
    assert state_result.data is not None
    assert len(state_result.data) == 1
    assert state_result.data[0]["tool_args"] == {"x": 1}
    assert state_result.data[0]["prompt_tokens"] == 0
    assert state_result.data[0]["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_save_action_step_allows_duplicate_step_number(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)

    first = await repo.save_action_step(
        step_id="step-1",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-1-THINK",
        local_step_number=1,
        user_id=USER_ID,
    )
    assert first.error is None

    duplicate = await repo.save_action_step(
        step_id="step-2",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-2-THINK",
        local_step_number=2,
        user_id=USER_ID,
    )

    assert duplicate.error is None


@pytest.mark.asyncio
async def test_save_action_step_returns_structured_constraint_error(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)

    invalid = await repo.save_action_step(
        step_id="step-invalid",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-0-THINK",
        local_step_number=0,
        user_id=USER_ID,
    )

    assert invalid.error is not None
    assert invalid.error_kind is RepositoryErrorKind.CONSTRAINT
    assert invalid.retryable is False


@pytest.mark.asyncio
async def test_save_action_step_returns_structured_transient_sqlite_error(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)

    class _LockedConnection:
        def __enter__(self) -> _LockedConnection:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    repo._connect = lambda: _LockedConnection()  # type: ignore[method-assign]  # noqa: SLF001

    result = await repo.save_action_step(
        step_id="step-locked",
        action_id=ACTION_ID,
        step_number=1,
        step_name="supervisor_think",
        step_type=StepType.LLM_OUTPUT,
        status=StepStatusType.SUCCESS,
        goal_handle="G1",
        short_step_id="G1-1-THINK",
        local_step_number=1,
        user_id=USER_ID,
    )

    assert result.error == "save_action_step: database is locked"
    assert result.error_kind is RepositoryErrorKind.TRANSIENT
    assert result.retryable is True


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_returns_later_same_step(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)
    _insert_checkpoint_step(
        db_path=db_path,
        step_id="latest-without-blocker",
        step_number=2,
        short_step_id="G1-2-THINK",
        created_at="2026-03-24T00:12:00Z",
        checkpoint={
            "current_approval_blockers": [],
            "next_action": None,
        },
    )
    _insert_checkpoint_step(
        db_path=db_path,
        step_id="matching-blocker",
        step_number=2,
        short_step_id="S-2-TOOL",
        created_at="2026-03-24T00:11:00Z",
        checkpoint={
            "current_approval_blockers": [
                {
                    "approval_session_id": "approval-1",
                    "tool_request_id": "request-1",
                }
            ],
        },
    )

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id=USER_ID,
        action_id=ACTION_ID,
        approval_session_id="approval-1",
        tool_request_id="request-1",
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["step_id"] == "latest-without-blocker"
    assert result.metadata == {"approval_anchor_step_id": "matching-blocker"}


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_matches_pending_request(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)
    _insert_checkpoint_step(
        db_path=db_path,
        step_id="matching-pending-request",
        step_number=2,
        short_step_id="S-2-TOOL",
        created_at="2026-03-24T00:11:00Z",
        checkpoint={
            "pending_approval_request": {
                "approval_session_id": "approval-1",
                "tool_request_id": "request-1",
            },
            "current_approval_blockers": [],
        },
    )

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id=USER_ID,
        action_id=ACTION_ID,
        approval_session_id="approval-1",
        tool_request_id="request-1",
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["step_id"] == "matching-pending-request"


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_returns_none_on_miss(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)
    _insert_checkpoint_step(
        db_path=db_path,
        step_id="other-approval",
        step_number=2,
        short_step_id="S-2-TOOL",
        created_at="2026-03-24T00:11:00Z",
        checkpoint={
            "current_approval_blockers": [
                {
                    "approval_session_id": "approval-other",
                    "tool_request_id": "request-other",
                }
            ],
        },
    )

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id=USER_ID,
        action_id=ACTION_ID,
        approval_session_id="approval-1",
        tool_request_id="request-1",
    )

    assert result.error is None
    assert result.data is None


@pytest.mark.asyncio
async def test_get_runtime_resume_context_selects_intervening_and_current_run(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)
    _insert_checkpoint_step(
        db_path=db_path,
        step_id="checkpoint-1",
        step_number=1,
        short_step_id="S-1-THINK",
        created_at="2026-03-24T00:01:00Z",
        checkpoint={"step": 2},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,"
            "updated_at,completed_at,heartbeat_at,next_event_seq) VALUES "
            "('process-2',?,'action','failed',?,'now','now','now','now',1)",
            (USER_ID, ACTION_ID),
        )
        connection.execute(
            "INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,"
            "accepted_sequence,local_step_number,short_step_id,step_type,step_name,"
            "status,goal_handle,retry_count,prompt_tokens,completion_tokens,"
            "user_message_id,user_message_json,user_request_text,adopted_process_id,"
            "created_at) VALUES ('user-2',?,?,2,1,2,'S-2-USER','user_request',"
            "'user_request','success','S',0,0,0,'message-2',?,'Second request',"
            "'process-2','2026-03-24T00:02:00Z')",
            (
                ACTION_ID,
                USER_ID,
                json.dumps(
                    {
                        "version": 1,
                        "message_id": "message-2",
                        "content": "Second request",
                        "language": "ja",
                        "images": [
                            {
                                "kind": "image",
                                "storage_path": "users/user-1/images/generic.png",
                            },
                            {
                                "kind": "image",
                                "storage_path": "users/user-1/images/screen.png",
                            },
                        ],
                    }
                ),
            ),
        )

    result = await repo.get_runtime_resume_context_for_user_step(
        user_id=USER_ID,
        action_id=ACTION_ID,
        current_user_step_number=3,
    )

    assert result.data is not None
    assert result.data.checkpoint_row is not None
    assert result.data.checkpoint_row["step_id"] == "checkpoint-1"
    (intervening,) = result.data.intervening_user_steps
    assert intervening.step_id == "user-2"
    assert intervening.message.language == "ja"
    assert tuple(
        image.model_dump(mode="json") for image in intervening.message.images
    ) == (
        {"kind": "image", "storage_path": "users/user-1/images/generic.png"},
        {"kind": "image", "storage_path": "users/user-1/images/screen.png"},
    )

    _insert_checkpoint_step(
        db_path=db_path,
        step_id="checkpoint-current-run",
        step_number=3,
        short_step_id="S-3-THINK",
        created_at="2026-03-24T00:03:00Z",
        checkpoint={"step": 4},
    )
    retry_result = await repo.get_runtime_resume_context_for_user_step(
        user_id=USER_ID,
        action_id=ACTION_ID,
        current_user_step_number=2,
    )
    assert retry_result.data is not None
    assert retry_result.data.checkpoint_row is not None
    assert retry_result.data.checkpoint_row["step_id"] == "checkpoint-current-run"
    assert retry_result.data.intervening_user_steps == ()


@pytest.mark.asyncio
async def test_get_action_steps_by_short_step_ids_returns_latest_rows_in_input_order(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)
    insert_action_step(
        db_path=db_path,
        step_id="step-old",
        short_step_id="G1-2-THINK",
        completed_at="2026-03-24T00:10:00Z",
        created_at="2026-03-24T00:10:00Z",
    )
    insert_action_step(
        db_path=db_path,
        step_id="step-latest",
        short_step_id="G1-2-THINK",
        completed_at="2026-03-24T00:11:00Z",
        created_at="2026-03-24T00:11:00Z",
    )
    insert_action_step(
        db_path=db_path,
        step_id="step-supervisor",
        short_step_id="S-1-THINK",
        completed_at="2026-03-24T00:12:00Z",
        created_at="2026-03-24T00:12:00Z",
    )

    result = await repo.get_action_steps_by_short_step_ids(
        user_id=USER_ID,
        action_id=ACTION_ID,
        short_step_ids=("S-1-THINK", "G1-2-THINK"),
    )

    assert result.error is None
    assert result.data is not None
    assert [row["step_id"] for row in result.data] == [
        "step-supervisor",
        "step-latest",
    ]


@pytest.mark.asyncio
async def test_get_action_steps_by_short_step_ids_returns_empty_on_miss(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)
    await save_success_action(repo)

    result = await repo.get_action_steps_by_short_step_ids(
        user_id=USER_ID,
        action_id=ACTION_ID,
        short_step_ids=("G9-9-TOOL",),
    )

    assert result.error is None
    assert result.data == []


def _insert_checkpoint_step(
    *,
    db_path: Path,
    step_id: str,
    step_number: int,
    short_step_id: str,
    created_at: str,
    checkpoint: dict[str, object],
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id,
                    action_id,
                    user_id,
                    step_number,
                    local_step_number,
                    short_step_id,
                    step_type,
                    step_name,
                    status,
                    goal_handle,
                    retry_count,
                    prompt_tokens,
                    completion_tokens,
                    completed_at,
                    created_at,
                    runtime_state_checkpoint,
                    runtime_state_checkpoint_version
                ) VALUES (?, ?, ?, ?, 1, ?, 'llm_output', 'supervisor_think', 'success', 'S', 0, 0, 0, ?, ?, ?, 1)
                """,
                (
                    step_id,
                    ACTION_ID,
                    USER_ID,
                    step_number,
                    short_step_id,
                    created_at,
                    created_at,
                    json.dumps(checkpoint),
                ),
            )
