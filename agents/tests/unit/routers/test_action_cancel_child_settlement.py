"""External Stop settles subagent children before it terminalizes the parent."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pantaray_agents.action_status import (
    FinalizeActionTerminalCommand,
    build_finalize_action_terminal_command,
    derive_action_terminal_failure,
)
from pantaray_agents.agents.action_agent.runtime.state import build_next_action
from pantaray_agents.application.action.cancellation_service import (
    ActionCancellationService,
    CancellationDeps,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    build_action_subagent_success_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    ActionTerminalRepository,
)
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    bind_local_runtime_db_execution_context,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    ExternalResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.schema.agent.action import ActionUserMessageInput
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_agents.utils.trace_context import TraceContextManager

from .test_action_cancel_service import (
    BUSY_TIMEOUT_MS,
    _bootstrap_runtime,
    _submit_and_start_turn,
)

SEEDED_AT = "2026-08-16T00:00:30Z"
LATE_TERMINAL_EVENT_ID = "late-terminal-event"


@pytest.fixture(autouse=True)
def _reset_active_session() -> Iterator[None]:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()


def _start_parent_with_root_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, SubmitActionMessageResult, str]:
    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Spawn a subagent and then stop the Action",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=turn.action_id,
        started_at=SEEDED_AT,
        allowed_tool_ids=("bash",),
    )
    return db_path, turn, context.execution_session_id


def _seed_child(
    db_path: Path, *, turn: SubmitActionMessageResult
) -> ActionSubagentJobPayload:
    payload = build_action_subagent_job_payload(
        {
            "job_id": "child-job-1",
            "process_id": "child-process-1",
            "user_id": "user-1",
            "action_id": turn.action_id,
            "parent_process_id": turn.process_id,
            "inference_profile_id": "action.subagent.luna",
            "action_context": "# Workspace Paths\nparent context",
            "task": "inspect the boundary",
            "context_refs": [],
            "resource_claim_ids": ["claim-1"],
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            enqueue_action_subagent_job_in_connection(
                connection=connection,
                payload=payload,
                scheduled_at=SEEDED_AT,
            )
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id="user-1",
                action_id=turn.action_id,
                parent_process_id=turn.process_id,
                child_process_id=payload["process_id"],
                acquired_at=SEEDED_AT,
                claims=(
                    ExternalResourceClaim(
                        claim_id="claim-1",
                        root_identity="workspace",
                        normalized_key="repository:1",
                    ),
                ),
            )
    return payload


def _claim_child(db_path: Path) -> None:
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_type="execute_action_subagent",
            owner_user_id="user-1",
            claimed_by="child-worker",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )


def _parent_snapshot(
    db_path: Path, turn: SubmitActionMessageResult
) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT action.status, process.status, job.status,
                       job.cancel_requested_at IS NOT NULL,
                       process.terminal_event_id IS NOT NULL
                FROM agent_actions AS action
                JOIN processes AS process ON process.process_id = ?
                JOIN jobs AS job ON job.job_id = ?
                WHERE action.action_id = ?
                """,
                (turn.process_id, turn.job_id, turn.action_id),
            ).fetchone()
        )


def _child_snapshot(
    db_path: Path, payload: ActionSubagentJobPayload
) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT job.status, process.status,
                       job.cancel_requested_at IS NOT NULL,
                       process.terminal_event_id IS NOT NULL,
                       (SELECT COUNT(*) FROM action_subagent_resource_claims
                        WHERE child_process_id = process.process_id
                          AND released_at IS NULL)
                FROM jobs AS job
                JOIN processes AS process ON process.process_id = job.process_id
                WHERE job.job_id = ?
                """,
                (payload["job_id"],),
            ).fetchone()
        )


def _pending_step_state(db_path: Path, step_id: str) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT step_number, adopted_process_id,
                       adoption_canceled_at IS NOT NULL
                FROM agent_action_steps WHERE step_id = ?
                """,
                (step_id,),
            ).fetchone()
        )


def _execution_session_status(db_path: Path, execution_session_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        return str(
            connection.execute(
                "SELECT status FROM execution_sessions WHERE execution_session_id = ?",
                (execution_session_id,),
            ).fetchone()[0]
        )


def _durable_user_turn(
    db_path: Path, turn: SubmitActionMessageResult
) -> tuple[str, str]:
    """Read the command identity the worker's terminal has to carry."""

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT user_message_id, created_at
            FROM agent_action_steps
            WHERE action_id = ? AND adopted_process_id = ?
              AND step_type = 'user_request' AND step_number IS NOT NULL
            ORDER BY step_number DESC
            LIMIT 1
            """,
            (turn.action_id, turn.process_id),
        ).fetchone()
    return str(row[0]), str(row[1])


def _late_worker_error_command(
    db_path: Path, turn: SubmitActionMessageResult
) -> FinalizeActionTerminalCommand:
    failure = derive_action_terminal_failure("error")
    assert failure is not None
    command_id, accepted_at = _durable_user_turn(db_path, turn)
    return build_finalize_action_terminal_command(
        process_completed_event_id=LATE_TERMINAL_EVENT_ID,
        user_id="user-1",
        suggestion_id=None,
        accepted_at=accepted_at,
        command_id=command_id,
        process_id=turn.process_id,
        action_id=turn.action_id,
        completed_at="2026-08-16T00:01:00Z",
        action_status="error",
        failure_code=failure.failure_code,
        failure_stage=failure.failure_stage,
        failure_message_public=failure.failure_message_public,
    )


async def _settle_child_and_write_parent_terminal(
    db_path: Path,
    turn: SubmitActionMessageResult,
    child: ActionSubagentJobPayload,
) -> str:
    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=child,
        result=build_action_subagent_success_result("late child report"),
    )
    result = await ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).finalize_action_job_terminal(
        command=_late_worker_error_command(db_path, turn),
        job_id=turn.job_id,
        runtime_state_checkpoint=None,
    )
    return str(result.action_status)


@pytest.mark.asyncio
async def test_running_child_defers_the_parent_terminal_and_keeps_the_root_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path, turn, root_session_id = _start_parent_with_root_session(
        tmp_path, monkeypatch
    )
    child = _seed_child(db_path, turn=turn)
    _claim_child(db_path)

    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
        expected_process_id=turn.process_id,
    )

    # The parent keeps its runtime authority: the Action is still processing,
    # the process has no terminal, and only the Stop fence is durable.
    assert _parent_snapshot(db_path, turn) == ("processing", "running", "running", 1, 0)
    # The running child keeps its claim until its own worker settles it.
    assert _child_snapshot(db_path, child) == ("running", "running", 1, 0, 1)
    # The root session still backs the child's broker authority.
    assert _execution_session_status(db_path, root_session_id) == "running"


@pytest.mark.asyncio
async def test_stop_fence_cancels_the_parent_terminal_once_the_child_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path, turn, _ = _start_parent_with_root_session(tmp_path, monkeypatch)
    child = _seed_child(db_path, turn=turn)
    _claim_child(db_path)
    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
        expected_process_id=turn.process_id,
    )

    action_status = await _settle_child_and_write_parent_terminal(db_path, turn, child)
    assert _child_snapshot(db_path, child) == ("canceled", "canceled", 1, 1, 0)

    # The worker's own terminal loses to the durable Stop fence.
    assert action_status == "canceled"
    assert _parent_snapshot(db_path, turn) == ("canceled", "canceled", "canceled", 1, 1)


@pytest.mark.asyncio
async def test_user_step_accepted_while_a_child_settles_is_marked_not_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path, turn, _ = _start_parent_with_root_session(tmp_path, monkeypatch)
    child = _seed_child(db_path, turn=turn)
    _claim_child(db_path)
    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
        expected_process_id=turn.process_id,
    )

    # The Action stays processing while the child settles, so a USER message
    # sent after the Stop transaction took its snapshot is still accepted.
    deferred = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=ExistingActionTarget(
                action_id=turn.action_id, expected_process_id=turn.process_id
            ),
            message=ActionUserMessageInput(
                message_id="message-2",
                content="Sent while the stopped Action waits for its child",
            ),
        )
    )
    assert deferred.disposition == "pending"

    assert (
        await _settle_child_and_write_parent_terminal(db_path, turn, child)
    ) == "canceled"

    # Stop discards every pending USER step, including the ones it could not
    # see when it recorded the fence.
    assert _pending_step_state(db_path, deferred.user_step_id) == (None, None, 1)


def _cancellation_service(projected_status: str) -> ActionCancellationService:
    return ActionCancellationService(
        CancellationDeps(
            repository=SimpleNamespace(
                get_action=AsyncMock(
                    return_value=SimpleNamespace(
                        error=None, data={"status": projected_status}
                    )
                )
            ),
            logger=logging.getLogger(__name__),
            now_provider=lambda: SEEDED_AT,
            build_agent_error=MagicMock(),
        )
    )


def _cancellation_state(action_id: str) -> dict[str, object]:
    return {
        "action_id": action_id,
        "user_id": "user-1",
        "status": "processing",
        "errors": [],
        "updated_at": SEEDED_AT,
        "next_action": build_next_action(tool=None, decided_at=SEEDED_AT),
        "cancel_check_max_consecutive_failures": 3,
        "cancel_check_failure_grace_seconds": 60,
    }


@pytest.mark.asyncio
async def test_canceled_action_stops_without_reading_the_stop_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pantaray_agents.application.action.cancellation_service as cancellation

    def _unavailable(**_kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cancellation, "_stop_fence_is_recorded", _unavailable)
    state = _cancellation_state("action-1")

    # A decided cancel must never depend on a second read that can fail.
    assert await _cancellation_service("canceled").check_cancellation(state) is True
    assert state["status"] == "canceled"


@pytest.mark.asyncio
async def test_worker_stops_on_the_stop_fence_while_the_action_is_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path, turn, _ = _start_parent_with_root_session(tmp_path, monkeypatch)
    _seed_child(db_path, turn=turn)
    _claim_child(db_path)
    service = _cancellation_service("processing")
    state = _cancellation_state(turn.action_id)

    with (
        bind_local_runtime_db_execution_context(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        ),
        TraceContextManager(local_job_id=turn.job_id),
    ):
        assert await service.check_cancellation(state) is False
        assert await cancel_service.execute_action_cancel(
            user_id="user-1",
            action_id=turn.action_id,
            reason="user_stop",
            expected_process_id=turn.process_id,
        )
        # The Action is still `processing`, so only the job fence can stop the
        # worker that owns the parent terminal.
        assert await service.check_cancellation(state) is True

    assert state["status"] == "canceled"
    assert state["next_action"] is None


@pytest.mark.asyncio
async def test_queued_child_settles_inside_the_single_stop_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path, turn, root_session_id = _start_parent_with_root_session(
        tmp_path, monkeypatch
    )
    child = _seed_child(db_path, turn=turn)

    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
        expected_process_id=turn.process_id,
    )

    assert _parent_snapshot(db_path, turn) == ("canceled", "canceled", "canceled", 1, 1)
    # The child settled through its own terminal transaction, which released
    # its claim rather than leaving a canceled process row behind.
    assert _child_snapshot(db_path, child) == ("canceled", "canceled", 1, 1, 0)
    assert _execution_session_status(db_path, root_session_id) == "canceled"
