from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.action_status import (
    ACTION_FAILURE_CODE_CANCELED,
    ACTION_FAILURE_MESSAGE_CANCELED,
    ACTION_FAILURE_STAGE_RUNNING_FAILED,
    ACTION_STATUS_CANCELED,
    build_finalize_action_terminal_command,
)
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    serialize_memory_draft,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
    ActionJobStartSkipCommand,
)
from pantaray_agents.local_runtime.runtime.action_malformed_stream_terminal import (
    ActionMalformedStreamTerminalRepository,
)
from pantaray_agents.local_runtime.runtime.action_queue import (
    build_local_action_enqueue_request,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    ActionTerminalRepository,
    finalize_action_job_terminal_in_connection,
)
from pantaray_agents.local_runtime.runtime.job_enqueue import enqueue_local_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    append_process_event_in_connection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.suggestion_state.shared import configure_connection
from pantaray_agents.schema.agent.action import ActionUserMessageInput
from pantaray_agents.tasks.action_user_message import serialize_action_user_message
from pantaray_agents.tasks.types import ActionJobPayload

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
ROOT_REQUEST_TEXT = (
    "do it\n\nSuggestion metadata:\n- Suggestion: sug-1\n"
    "- Approved at: 2026-03-24T00:00:00Z"
)


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(
                    user_id,
                    ui_language,
                    created_at,
                    updated_at
                ) VALUES ('user-1', 'ja', '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    has_suggestion,
                    interaction_contract,
                    created_at,
                    updated_at
                ) VALUES (?, ?, 'success', 'answer', 'prompt', 'response', 'prompt', 'v1', 1, 'action_offer', '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z')
                """,
                ("sug-1", "user-1"),
            )
    return db_path


def _enqueue_and_claim_action(
    db_path: Path, *, suggestion_id: str | None = "sug-1"
) -> None:
    user_message_json = (
        '{"version":1,"message_id":"command-1","content":"do it",'
        '"images":[],"suggestion_approval":{"suggestion_id":"sug-1",'
        '"approved_at":"2026-03-24T00:00:00Z"}}'
        if suggestion_id is not None
        else serialize_action_user_message(
            ActionUserMessageInput(message_id="command-1", content="do it")
        )
    )
    user_request_text = ROOT_REQUEST_TEXT if suggestion_id is not None else "do it"
    payload: ActionJobPayload = build_action_job_payload(
        {
            "job_id": "job-1",
            "process_id": "process-1",
            "action_id": "action-1",
            "user_id": "user-1",
            "continuation_ref": {
                "kind": "user_step",
                "user_step_id": "user-step-1",
            },
        }
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, suggestion_id, initial_user_message_id,
                    execution_target_json, status, final_output, prompt_name,
                    prompt_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', '', ?, ?, ?, ?)
                """,
                (
                    "action-1",
                    "user-1",
                    suggestion_id,
                    "command-1",
                    '{"kind":"scratch"}',
                    "action/executing",
                    "1.0",
                    "2026-03-24T00:00:01Z",
                    "2026-03-24T00:00:01Z",
                ),
            )
            enqueue_local_job(
                connection=connection,
                request=build_local_action_enqueue_request(
                    payload,
                    scheduled_at="2026-03-24T00:00:01Z",
                    suggestion_id=suggestion_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, local_step_number,
                    short_step_id, step_type, step_name, status, goal_handle,
                    retry_count, prompt_tokens, completion_tokens,
                    user_message_id, user_message_json, user_request_text,
                    accepted_sequence, adopted_process_id,
                    started_at, completed_at, created_at
                ) VALUES (?, ?, ?, 1, 1, 'S-1-USER', 'user_request',
                          'user_request', 'success', 'S', 0, 0, 0, ?, ?, ?,
                          1, 'process-1', ?, ?, ?)
                """,
                (
                    "user-step-1",
                    "action-1",
                    "user-1",
                    "command-1",
                    user_message_json,
                    user_request_text,
                    "2026-03-24T00:00:01Z",
                    "2026-03-24T00:00:01Z",
                    "2026-03-24T00:00:01Z",
                ),
            )
    claimed = claim_next_pending_action_job(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id="user-1",
        claimed_by="worker-1",
    )
    assert claimed is not None
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                "UPDATE agent_actions SET status = 'processing' WHERE action_id = 'action-1'"
            )
            if suggestion_id is not None:
                connection.execute(
                    """
                    UPDATE agent_suggestions
                    SET user_reaction = 'accepted', action_status = 'processing',
                        action_command_id = 'command-1', action_process_id = 'process-1',
                        action_started_at = '2026-03-24T00:00:01Z'
                    WHERE suggestion_id = 'sug-1'
                    """
                )


def _action_memory_draft_json(db_path: Path) -> str:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            node = ensure_preparing_node(
                connection=connection,
                user_id="user-1",
                source="action",
                source_record_id="action-1",
            )
    draft = create_memory_draft(
        user_id="user-1",
        owner_node_id=node.node_id,
        base_revision_id=None,
        documents=(MemoryDocument("body.md", "done"),),
    )
    return serialize_memory_draft(draft).model_dump_json()


def _seed_pending_approval_sessions(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.executemany(
                """
                INSERT INTO approval_sessions(
                    approval_session_id, user_id, action_id, tool_request_id,
                    tool_id, intent_class, approval_source, status,
                    approved_capabilities_json, command_summary_json,
                    requested_at, created_at
                ) VALUES (?, 'user-1', 'action-1', ?, 'bash',
                          'process_exec_local', 'prompt', 'pending', '{}', '{}', ?, ?)
                """,
                [
                    (
                        "approval-1",
                        "tool-request-1",
                        "2026-03-24T00:02:00Z",
                        "2026-03-24T00:02:00Z",
                    ),
                    (
                        "approval-2",
                        "tool-request-2",
                        "2026-03-24T00:02:01Z",
                        "2026-03-24T00:02:01Z",
                    ),
                ],
            )


def _seed_pending_user_steps(db_path: Path) -> None:
    messages = (
        ("pending-1", "message-2", "two", 2),
        ("pending-2", "message-3", "three", 3),
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.executemany(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_type, step_name, status,
                goal_handle, retry_count, prompt_tokens, completion_tokens,
                user_message_id, user_message_json, user_request_text,
                accepted_sequence, expected_process_id,
                started_at, completed_at, created_at
            ) VALUES (?, 'action-1', 'user-1', 'user_request', 'user_request',
                      'success', 'S', 0, 0, 0, ?, ?, ?, ?, 'process-1',
                      '2026-03-24T00:00:02Z', '2026-03-24T00:00:02Z',
                      '2026-03-24T00:00:02Z')
            """,
            (
                (
                    step_id,
                    message_id,
                    serialize_action_user_message(
                        ActionUserMessageInput(message_id=message_id, content=content)
                    ),
                    content,
                    sequence,
                )
                for step_id, message_id, content, sequence in messages
            ),
        )


def _terminal_checkpoint(status: str) -> dict[str, object]:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="sug-1",
        action_id="action-1",
        started_at="2026-03-24T00:00:01Z",
        max_steps=20,
        max_tool_steps=10,
        token_budget=None,
    )
    state["phase"] = "executing"
    projected = project_persisted_user_request_step(
        state,
        step_id="user-step-1",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text=ROOT_REQUEST_TEXT,
        occurred_at="2026-03-24T00:00:01Z",
        history_phase="executing",
    )
    projected["status"] = status  # type: ignore[typeddict-item]
    projected["updated_at"] = "2026-03-24T00:03:00Z"
    projected["final_output"] = "done" if status == "success" else None
    return build_runtime_state_checkpoint(projected)


def _malformed_terminal_repository(
    db_path: Path,
) -> ActionMalformedStreamTerminalRepository:
    return ActionMalformedStreamTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )


@pytest.mark.parametrize("malformed_event_name", ["stream_end"])
def test_malformed_stream_closes_canonical_action_and_runtime_atomically(
    tmp_path: Path,
    malformed_event_name: str,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    _seed_pending_user_steps(db_path)
    malformed_event_id = f"malformed-{malformed_event_name}"
    with sqlite3.connect(db_path) as connection, connection:
        append_process_event_in_connection(
            connection=connection,
            process_id="process-1",
            event_id=malformed_event_id,
            event_name=malformed_event_name,
            payload={},
            created_at="2026-03-24T00:02:30Z",
        )

    repository = _malformed_terminal_repository(db_path)
    result = repository.finalize(
        user_id="user-1",
        suggestion_id="sug-1",
        action_id="action-1",
        process_id="process-1",
        command_id="command-1",
        malformed_event_id=malformed_event_id,
        completed_at="2026-03-24T00:03:00Z",
        failure_code="ACTION_STREAM_PAYLOAD_INVALID",
        failure_message_public="Action stream payload is invalid.",
    )
    retry_result = repository.finalize(
        user_id="user-1",
        suggestion_id="sug-1",
        action_id="action-1",
        process_id="process-1",
        command_id="command-1",
        malformed_event_id=malformed_event_id,
        completed_at="2026-03-24T00:04:00Z",
        failure_code="ACTION_STREAM_PAYLOAD_INVALID",
        failure_message_public="Action stream payload is invalid.",
    )

    with sqlite3.connect(db_path) as connection:
        runtime = connection.execute(
            """
            SELECT process.status, process.completed_at, process.current_job_id,
                   process.terminal_event_id, process.updated_at,
                   process.heartbeat_at, job.status, job.completed_at,
                   job.heartbeat_at, job.error_code, attempt.status,
                   attempt.completed_at, attempt.error_code
            FROM processes AS process
            JOIN jobs AS job ON job.process_id = process.process_id
            JOIN job_attempts AS attempt
              ON attempt.job_id = job.job_id AND attempt.attempt_number = job.attempt
            WHERE process.process_id = 'process-1'
            """
        ).fetchone()
        projection = connection.execute(
            """
            SELECT action.status, suggestion.action_status,
                   suggestion.action_process_id, suggestion.action_command_id,
                   action.updated_at, suggestion.updated_at
            FROM agent_actions AS action
            JOIN agent_suggestions AS suggestion
              ON suggestion.suggestion_id = action.suggestion_id
            WHERE action.action_id = 'action-1'
            """
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_id, event_name, payload_json, created_at
            FROM process_events WHERE process_id = 'process-1'
            """
        ).fetchall()
        public_events = connection.execute(
            """
            SELECT event_id, event_name, created_at FROM agent_process_events
            WHERE action_id = 'action-1' AND event_name = 'process_completed'
            """
        ).fetchall()
        pending_steps = connection.execute(
            """
            SELECT adoption_canceled_at FROM agent_action_steps
            WHERE step_id LIKE 'pending-%' ORDER BY accepted_sequence
            """
        ).fetchall()

    completed_at = "2026-03-24T00:03:00.000000Z"
    recency_at = "2026-03-24T00:03:00.000Z"
    assert result.terminal_status == "error"
    assert result.terminal_event_id == "process-1:invalid-payload"
    assert result.emit_invalid_payload_error is True
    assert retry_result.terminal_status == "error"
    assert retry_result.terminal_event_id == result.terminal_event_id
    assert retry_result.emit_invalid_payload_error is False
    assert runtime == (
        "failed",
        completed_at,
        None,
        "process-1:invalid-payload",
        completed_at,
        completed_at,
        "failed",
        completed_at,
        completed_at,
        "ACTION_STREAM_PAYLOAD_INVALID",
        "failed",
        completed_at,
        "ACTION_STREAM_PAYLOAD_INVALID",
    )
    assert projection == (
        "error",
        "error",
        "process-1",
        "command-1",
        recency_at,
        recency_at,
    )
    terminal_events = [event for event in events if event[1] == "stream_end"]
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == "process-1:invalid-payload"
    assert terminal_events[0][3] == completed_at
    assert json.loads(str(terminal_events[0][2]))["persisted_sequence"] > 0
    assert malformed_event_id not in {event[0] for event in events}
    assert public_events == [
        ("process-1:invalid-payload", "process_completed", completed_at)
    ]
    assert len(pending_steps) == 2
    assert all(step[0] is not None for step in pending_steps)


def test_malformed_standalone_stream_replaces_raw_terminal_and_replays_winner(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path, suggestion_id=None)
    with sqlite3.connect(db_path) as connection, connection:
        append_process_event_in_connection(
            connection=connection,
            process_id="process-1",
            event_id="malformed-standalone",
            event_name="stream_end",
            payload={},
            created_at="2026-03-24T00:02:30Z",
        )

    repository = _malformed_terminal_repository(db_path)
    result = repository.finalize(
        user_id="user-1",
        suggestion_id=None,
        action_id="action-1",
        process_id="process-1",
        command_id="command-1",
        malformed_event_id="malformed-standalone",
        completed_at="2026-03-24T00:03:00Z",
        failure_code="ACTION_STREAM_PAYLOAD_INVALID",
        failure_message_public="Action stream payload is invalid.",
    )
    replay = repository.finalize(
        user_id="user-1",
        suggestion_id=None,
        action_id="action-1",
        process_id="process-1",
        command_id="command-1",
        malformed_event_id="malformed-standalone",
        completed_at="2026-03-24T00:04:00Z",
        failure_code="ACTION_STREAM_PAYLOAD_INVALID",
        failure_message_public="Action stream payload is invalid.",
    )

    with sqlite3.connect(db_path) as connection:
        settled = connection.execute(
            """
            SELECT action.status, process.status, job.status, attempt.status,
                   process.current_job_id, process.terminal_event_id
            FROM agent_actions AS action
            JOIN processes AS process ON process.action_id = action.action_id
            JOIN jobs AS job ON job.process_id = process.process_id
            JOIN job_attempts AS attempt
              ON attempt.job_id = job.job_id AND attempt.attempt_number = job.attempt
            WHERE action.action_id = 'action-1'
            """
        ).fetchone()
        terminal_events = connection.execute(
            """
            SELECT event_id, payload_json FROM process_events
            WHERE process_id = 'process-1' AND event_name = 'stream_end'
            """
        ).fetchall()
        public_events = connection.execute(
            """
            SELECT event_id, suggestion_id, payload FROM agent_process_events
            WHERE action_id = 'action-1' AND event_name = 'process_completed'
            """
        ).fetchall()

    assert result.emit_invalid_payload_error is True
    assert replay.terminal_status == "error"
    assert replay.terminal_event_id == result.terminal_event_id
    assert replay.emit_invalid_payload_error is False
    assert settled == (
        "error",
        "failed",
        "failed",
        "failed",
        None,
        "process-1:invalid-payload",
    )
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == result.terminal_event_id
    assert "suggestion_id" not in json.loads(str(terminal_events[0][1]))
    assert len(public_events) == 1
    assert public_events[0][:2] == (result.terminal_event_id, None)
    assert "suggestion_id" not in json.loads(str(public_events[0][2]))["data"]


@pytest.mark.asyncio
async def test_malformed_stream_preserves_existing_canonical_terminal(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    command = build_finalize_action_terminal_command(
        process_completed_event_id="existing-terminal",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:02:00Z",
        action_status="error",
        failure_code="ACTION_FAILED",
        failure_stage="running_failed",
        failure_message_public="Action execution failed.",
    )
    await ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=None,
    )

    def read_terminal_state() -> tuple[tuple[tuple[object, ...], ...], ...]:
        queries = (
            "SELECT status,completed_at,current_job_id,terminal_event_id,updated_at,heartbeat_at FROM processes WHERE process_id='process-1'",
            "SELECT status,completed_at,heartbeat_at,error_code FROM jobs WHERE job_id='job-1'",
            "SELECT status,completed_at,error_code FROM job_attempts WHERE job_id='job-1'",
            "SELECT event_id,event_name,payload_json,created_at FROM process_events WHERE process_id='process-1' ORDER BY event_seq",
            "SELECT event_id,event_name,payload,created_at FROM agent_process_events WHERE action_id='action-1' ORDER BY sequence",
            "SELECT status,final_output,error,updated_at FROM agent_actions WHERE action_id='action-1'",
            "SELECT action_status,action_failure_code,action_process_id,action_command_id,updated_at FROM agent_suggestions WHERE suggestion_id='sug-1'",
        )
        with sqlite3.connect(db_path) as connection:
            return tuple(
                tuple(connection.execute(query).fetchall()) for query in queries
            )

    before = read_terminal_state()

    result = _malformed_terminal_repository(db_path).finalize(
        user_id="user-1",
        suggestion_id="sug-1",
        action_id="action-1",
        process_id="process-1",
        command_id="command-1",
        malformed_event_id="missing-malformed-event",
        completed_at="2026-03-24T00:03:00Z",
        failure_code="ACTION_STREAM_PAYLOAD_INVALID",
        failure_message_public="Action stream payload is invalid.",
    )

    after = read_terminal_state()
    assert result.terminal_status == "error"
    assert result.terminal_event_id == "existing-terminal"
    assert result.emit_invalid_payload_error is False
    assert after == before


def test_malformed_stream_rejects_wrong_command_before_writing(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)

    with pytest.raises(MigrationError, match="USER step is not unique"):
        _malformed_terminal_repository(db_path).finalize(
            user_id="user-1",
            suggestion_id="sug-1",
            action_id="action-1",
            process_id="process-1",
            command_id="other-command",
            malformed_event_id="missing-malformed-event",
            completed_at="2026-03-24T00:03:00Z",
            failure_code="ACTION_STREAM_PAYLOAD_INVALID",
            failure_message_public="Action stream payload is invalid.",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, terminal_event_id FROM processes WHERE process_id='process-1'"
        ).fetchone() == ("running", None)
        assert connection.execute(
            "SELECT COUNT(*) FROM process_events WHERE process_id='process-1'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_finalize_action_job_terminal_closes_runtime_state_and_enqueues_pipeline(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    repo = ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    command = build_finalize_action_terminal_command(
        process_completed_event_id="event-1",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:03:00Z",
        action_status="success",
        failure_code=None,
        error_payload=None,
        final_output="done",
        failure_stage=None,
        failure_message_public=None,
        final_prompt_text="final prompt",
        prompt_name="action/executing",
        prompt_version="1.0",
        total_steps=3,
        total_llm_steps=2,
        total_tool_steps=1,
        total_prompt_tokens=20,
        total_completion_tokens=10,
        memory_draft_json=_action_memory_draft_json(db_path),
    )

    result = await repo.finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=None,
    )

    assert result.action_status == "success"
    with sqlite3.connect(db_path) as connection:
        job_row = connection.execute(
            """
            SELECT status, completed_at
            FROM jobs
            WHERE job_id = 'job-1'
            """
        ).fetchone()
        process_row = connection.execute(
            """
            SELECT status, completed_at, current_job_id
            FROM processes
            WHERE process_id = 'process-1'
            """
        ).fetchone()
        attempt_row = connection.execute(
            """
            SELECT status, completed_at
            FROM job_attempts
            WHERE job_id = 'job-1'
            """
        ).fetchone()
        stream_end_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM process_events
            WHERE process_id = 'process-1' AND event_name = 'stream_end'
            """
        ).fetchone()
        stream_end_payload_row = connection.execute(
            """
            SELECT payload_json
            FROM process_events
            WHERE process_id = 'process-1' AND event_name = 'stream_end'
            LIMIT 1
            """
        ).fetchone()
        pipeline_row = connection.execute(
            """
            SELECT job_type, status, logical_key
            FROM jobs
            WHERE job_type = 'post_action_pipeline'
            """,
        ).fetchone()
        trigger_row = connection.execute(
            """
                SELECT event_name, json_extract(payload_json, '$.source_id'), json_extract(payload_json, '$.action_id'),
                       json_extract(payload_json, '$.action_completed_at'),
                       json_extract(payload_json, '$.source_action_revision_id'),
                       json_extract(payload_json, '$.turn_start_step_number'), json_extract(payload_json, '$.turn_end_step_number'),
                       json_extract(payload_json, '$.action_prompt_name'), json_extract(payload_json, '$.action_prompt_version'),
                       json_extract(payload_json, '$.suggestion_id')
                FROM hook_inbox
            """
        ).fetchone()
        next_event_seq_row = connection.execute(
            """
            SELECT next_event_seq
            FROM processes
            WHERE process_id = 'process-1'
            """
        ).fetchone()

    assert job_row == ("completed", command.completed_at)
    assert process_row == ("completed", command.completed_at, None)
    assert attempt_row == ("completed", command.completed_at)
    assert stream_end_count == (1,)
    assert stream_end_payload_row is not None
    stream_end_payload = json.loads(str(stream_end_payload_row[0]))
    assert stream_end_payload["status"] == "success"
    assert stream_end_payload["final_output"] == "done"
    assert stream_end_payload["persisted_sequence"] == (
        result.process_completed_sequence
    )
    assert pipeline_row is None
    assert trigger_row is not None
    assert trigger_row[:4] == (
        "action.turn_ended",
        "action-1:1",
        "action-1",
        command.completed_at,
    )
    assert str(trigger_row[4]).startswith("rev_")
    assert trigger_row[5:] == (1, 1, "action/executing", "1.0", "sug-1")
    assert next_event_seq_row == (2,)


@pytest.mark.asyncio
async def test_finalize_error_writes_canonical_failure_to_stream_end(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    _seed_pending_approval_sessions(db_path)
    repo = ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    command = build_finalize_action_terminal_command(
        process_completed_event_id="event-error",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:03:00Z",
        action_status="error",
        failure_code="ACTION_FAILED",
        error_payload=None,
        final_output=None,
        failure_stage="running_failed",
        failure_message_public="Action execution failed.",
    )

    result = await repo.finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=None,
    )

    with sqlite3.connect(db_path) as connection:
        payload_json = connection.execute(
            """
            SELECT payload_json
            FROM process_events
            WHERE process_id = 'process-1' AND event_name = 'stream_end'
            """
        ).fetchone()[0]
        approval_rows = connection.execute(
            """
            SELECT approval_session_id, status, decided_at
            FROM approval_sessions
            ORDER BY approval_session_id
            """
        ).fetchall()
    payload = json.loads(str(payload_json))
    assert payload["status"] == "error"
    assert payload["failure_code"] == "ACTION_FAILED"
    assert payload["failure_stage"] == "running_failed"
    assert payload["failure_message_public"] == "Action execution failed."
    assert payload["persisted_sequence"] == result.process_completed_sequence
    assert approval_rows == [
        ("approval-1", "interrupted", command.completed_at),
        ("approval-2", "interrupted", command.completed_at),
    ]


@pytest.mark.parametrize(
    ("status", "has_checkpoint", "payload_mismatch"),
    [
        ("success", True, False),
        ("error", True, False),
        ("error", False, False),
        ("error", True, True),
    ],
)
@pytest.mark.asyncio
async def test_terminal_handoff_projects_or_closes_two_pending_users_atomically(
    tmp_path: Path,
    status: str,
    has_checkpoint: bool,
    payload_mismatch: bool,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    _seed_pending_user_steps(db_path)
    command = build_finalize_action_terminal_command(
        process_completed_event_id=f"event-{status}-{has_checkpoint}",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:03:00Z",
        action_status=status,  # type: ignore[arg-type]
        failure_code="ACTION_FAILED" if status == "error" else None,
        final_output="done" if status == "success" else None,
        failure_stage="running_failed" if status == "error" else None,
        failure_message_public=(
            "Action execution failed." if status == "error" else None
        ),
        memory_draft_json=(
            _action_memory_draft_json(db_path) if status == "success" else None
        ),
    )
    repo = ActionTerminalRepository(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    checkpoint = _terminal_checkpoint(status) if has_checkpoint else None
    if payload_mismatch:
        with sqlite3.connect(db_path) as connection, connection:
            connection.execute(
                "UPDATE job_payloads SET payload_json="
                "json_set(payload_json,'$.user_id','other-user') WHERE job_id='job-1'"
            )
        with pytest.raises(MigrationError, match="envelope"):
            await repo.finalize_action_job_terminal(
                command=command,
                job_id="job-1",
                runtime_state_checkpoint=checkpoint,
            )
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                """SELECT actions.status,jobs.status,
                          (SELECT COUNT(*) FROM process_events WHERE event_name='stream_end'),
                          (SELECT COUNT(*) FROM agent_action_steps
                           WHERE adoption_canceled_at IS NOT NULL)
                   FROM agent_actions AS actions JOIN jobs
                     ON jobs.logical_key=actions.action_id WHERE jobs.job_id='job-1'"""
            ).fetchone() == ("processing", "running", 0, 0)
        return

    await repo.finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=checkpoint,
    )

    with sqlite3.connect(db_path) as connection:
        action = connection.execute(
            "SELECT status, final_output, error FROM agent_actions"
        ).fetchone()
        pending = connection.execute(
            """SELECT step_id,step_number,adoption_canceled_at,adopted_process_id,
                      runtime_state_checkpoint FROM agent_action_steps
               WHERE step_id LIKE 'pending-%' ORDER BY accepted_sequence"""
        ).fetchall()
        successors = connection.execute(
            """SELECT jobs.job_id,jobs.status,processes.process_id,processes.status,
                      payloads.payload_json FROM jobs JOIN processes USING(process_id)
               JOIN job_payloads AS payloads USING(job_id)
               WHERE jobs.job_type='execute_action' AND jobs.job_id<>'job-1'"""
        ).fetchall()
        adopted_events = connection.execute(
            """SELECT json_extract(payload,'$.data.step_id')
               FROM agent_process_events WHERE event_name='action_message_adopted'"""
        ).fetchall()
        trigger_row = connection.execute(
            """
            SELECT json_extract(payload_json, '$.source_action_revision_id'),
                   json_extract(payload_json, '$.turn_start_step_number'), json_extract(payload_json, '$.turn_end_step_number')
            FROM hook_inbox WHERE event_name = 'action.turn_ended'
            """
        ).fetchone()
        assert trigger_row is not None
        # Every terminal binds its own turn; only a successful one has a
        # published revision to reference.
        assert (trigger_row[0] is not None) == (status == "success")
        assert trigger_row[1:] == (1, 1)

    if has_checkpoint:
        assert action == ("queued", "", None)
        assert len(successors) == 1
        successor_process_id = successors[0][2]
        assert successors[0][1:4] == ("queued", successor_process_id, "enqueued")
        assert json.loads(successors[0][4])["continuation_ref"] == {
            "kind": "user_step",
            "user_step_id": "pending-1",
        }
        assert [row[1:4] for row in pending] == [
            (2, None, successor_process_id),
            (3, None, successor_process_id),
        ]
        assert pending[0][4] is None
        checkpoint = json.loads(pending[1][4])
        assert (checkpoint["phase"], checkpoint["status"], checkpoint["step"]) == (
            "init",
            "processing",
            4,
        )
        assert adopted_events == [("pending-1",), ("pending-2",)]
    else:
        assert action[0] == "error"
        assert successors == [] and adopted_events == []
        assert all(row[1] is None and row[2] is not None for row in pending)


def test_skipped_action_start_preserves_stop_owned_terminal(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    stop_command = build_finalize_action_terminal_command(
        process_completed_event_id="stop-terminal",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:02:00Z",
        action_status=ACTION_STATUS_CANCELED,
        failure_code=ACTION_FAILURE_CODE_CANCELED,
        failure_stage=ACTION_FAILURE_STAGE_RUNNING_FAILED,
        failure_message_public=ACTION_FAILURE_MESSAGE_CANCELED,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, BUSY_TIMEOUT_MS)
        with immediate_transaction(connection):
            result = finalize_action_job_terminal_in_connection(
                connection=connection,
                command=stop_command,
                job_id="job-1",
            )

    repository = ActionJobRuntimeRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    skip_command = ActionJobStartSkipCommand(
        job_id="job-1",
        process_id="process-1",
        user_id="user-1",
        action_id="action-1",
        suggestion_id="sug-1",
        command_id="command-1",
        accepted_at="2026-03-24T00:00:00Z",
        completed_at="2026-03-24T00:03:00Z",
        failure_stage="start_failed",
        outcome="already_processing",
    )
    repository.finalize_skipped_action_start(command=skip_command)

    with sqlite3.connect(db_path) as connection:
        action = connection.execute(
            "SELECT status, updated_at FROM agent_actions WHERE action_id = 'action-1'"
        ).fetchone()
        suggestion = connection.execute(
            "SELECT action_status, updated_at FROM agent_suggestions WHERE suggestion_id = 'sug-1'"
        ).fetchone()
        process = connection.execute(
            "SELECT status, completed_at, terminal_event_id FROM processes WHERE process_id = 'process-1'"
        ).fetchone()
        internal_events = connection.execute(
            "SELECT event_id, event_name, payload_json FROM process_events WHERE process_id = 'process-1'"
        ).fetchall()
        public_events = connection.execute(
            "SELECT event_id, suggestion_id, action_id, sequence, event_name FROM agent_process_events WHERE action_id = 'action-1'"
        ).fetchall()

    completed_at = "2026-03-24T00:02:00.000000Z"
    recency_at = "2026-03-24T00:02:00.000Z"
    assert action == ("canceled", recency_at)
    assert suggestion == ("canceled", recency_at)
    assert process == ("canceled", completed_at, "stop-terminal")
    assert [(row[0], row[1]) for row in internal_events] == [
        ("stop-terminal", "stream_end")
    ]
    internal_payload = json.loads(internal_events[0][2])
    assert internal_payload["persisted_sequence"] == result.process_completed_sequence
    assert "physical_run_only" not in internal_payload
    assert public_events == [
        (
            "stop-terminal",
            "sug-1",
            "action-1",
            result.process_completed_sequence,
            "process_completed",
        )
    ]

    internal_payload["persisted_sequence"] = result.process_completed_sequence + 1
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE process_events SET payload_json = ? WHERE event_id = 'stop-terminal'",
            (json.dumps(internal_payload),),
        )
    with pytest.raises(MigrationError, match="Prior Action run"):
        repository.finalize_skipped_action_start(command=skip_command)
    with sqlite3.connect(db_path) as connection:
        skipped_count = connection.execute(
            "SELECT COUNT(*) FROM process_events WHERE event_name = 'action_start_skipped'"
        ).fetchone()
    assert skipped_count == (0,)


@pytest.mark.asyncio
async def test_finalize_action_job_terminal_is_idempotent_for_same_event(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    _seed_pending_user_steps(db_path)
    repo = ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    command = build_finalize_action_terminal_command(
        process_completed_event_id="event-1",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id="process-1",
        action_id="action-1",
        completed_at="2026-03-24T00:03:00Z",
        action_status="success",
        failure_code=None,
        error_payload=None,
        final_output="done",
        failure_stage=None,
        failure_message_public=None,
        final_prompt_text="final prompt",
        prompt_name="action/executing",
        prompt_version="1.0",
        total_steps=3,
        total_llm_steps=2,
        total_tool_steps=1,
        total_prompt_tokens=20,
        total_completion_tokens=10,
        memory_draft_json=_action_memory_draft_json(db_path),
    )
    checkpoint = _terminal_checkpoint("success")
    first_result = await repo.finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=checkpoint,
    )
    _seed_pending_approval_sessions(db_path)
    second_result = await repo.finalize_action_job_terminal(
        command=command,
        job_id="job-1",
        runtime_state_checkpoint=checkpoint,
    )

    with sqlite3.connect(db_path) as connection:
        stream_end_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM process_events
            WHERE process_id = 'process-1' AND event_name = 'stream_end'
            """
        ).fetchone()
        pipeline_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE job_type = 'post_action_pipeline'
            """,
        ).fetchone()
        trigger_count = connection.execute(
            "SELECT COUNT(*) FROM hook_inbox WHERE event_name = 'action.turn_ended'"
        ).fetchone()
        successor_count = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type='execute_action'"
        ).fetchone()
        approvals = connection.execute(
            "SELECT status FROM approval_sessions ORDER BY approval_session_id"
        ).fetchall()

    assert stream_end_count == (1,)
    assert pipeline_count == (0,)
    assert trigger_count == (1,)
    assert successor_count == (2,)
    assert approvals == [("pending",), ("pending",)]
    assert second_result.process_completed_sequence == (
        first_result.process_completed_sequence
    )
