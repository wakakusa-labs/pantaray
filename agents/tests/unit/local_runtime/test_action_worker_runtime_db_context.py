from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_job_recovery import (
    ACTION_JOB_OPERATIONAL_RETRY_ERROR_CODE,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
    ActionJobStartSkipCommand,
    ActionStartSkipOutcome,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    NewActionTarget,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.action_queue import (
    build_local_action_enqueue_request,
)
from pantaray_agents.local_runtime.runtime.job_enqueue import enqueue_local_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
    finalize_local_action_job,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
    repair_inflight_jobs_for_startup,
)
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.tasks.action_user_message import (
    render_action_user_request_text,
    serialize_action_user_message,
)
from pantaray_agents.tasks.types import ActionContinuationRef, ActionJobRuntimePayload

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
STARTED_AT = "2026-08-16T01:00:00Z"


def _create_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE agent_actions (
                action_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                suggestion_id TEXT,
                initial_user_message_id TEXT NOT NULL,
                status TEXT NOT NULL,
                execution_target_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE agent_action_steps (
                step_id TEXT PRIMARY KEY,
                action_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                local_step_number INTEGER,
                short_step_id TEXT,
                step_type TEXT NOT NULL,
                step_name TEXT,
                status TEXT NOT NULL,
                user_message_id TEXT,
                user_message_json TEXT,
                user_request_text TEXT,
                llm_response_text TEXT,
                runtime_state_checkpoint TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE processes (
                process_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                action_id TEXT,
                suggestion_id TEXT,
                current_job_id TEXT,
                next_event_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                heartbeat_at TEXT,
                completed_at TEXT,
                terminal_event_id TEXT
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                process_id TEXT,
                status TEXT NOT NULL,
                logical_key TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                heartbeat_at TEXT,
                completed_at TEXT,
                error_code TEXT
            );
            CREATE TABLE job_payloads (
                job_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE job_attempts (
                job_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT
            );
            CREATE TABLE process_events (
                process_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                chunk_index INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(process_id, event_seq),
                UNIQUE(process_id, event_id)
            );
            CREATE TABLE agent_process_events (
                event_id TEXT PRIMARY KEY,
                suggestion_id TEXT,
                user_id TEXT NOT NULL,
                action_id TEXT,
                sequence INTEGER NOT NULL,
                event_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE approval_sessions (
                approval_session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                tool_request_id TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_at TEXT
            );
            """
        )


def _payload(
    *,
    continuation_ref: ActionContinuationRef | None = None,
) -> ActionJobRuntimePayload:
    return {
        "job_id": "job-1",
        "process_id": "process-1",
        "action_id": "action-1",
        "user_id": "user-1",
        "continuation_ref": continuation_ref
        or {"kind": "user_step", "user_step_id": "user-step-1"},
    }


def _message(*, suggestion_id: str | None = None) -> ActionUserMessageInput:
    approval = (
        SuggestionApprovalInput(
            suggestion_id=suggestion_id,
            approved_at="2026-08-16T00:30:00Z",
            summary="Approved work",
        )
        if suggestion_id is not None
        else None
    )
    return ActionUserMessageInput(
        message_id="message-1",
        content="Perform the requested work",
        images=(ImageInput(storage_path="images/input.png"),),
        language="ja",
        suggestion_approval=approval,
    )


def _insert_running_envelope(
    db_path: Path,
    *,
    payload: ActionJobRuntimePayload,
    message: ActionUserMessageInput,
    action_status: str = "queued",
    suggestion_id: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id, user_id, suggestion_id, initial_user_message_id, status,
                execution_target_json, updated_at
            ) VALUES (?, ?, ?, 'message-1', ?, '{"kind":"scratch"}', ?)
            """,
            (
                payload["action_id"],
                payload["user_id"],
                suggestion_id,
                action_status,
                "2026-08-16T00:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, local_step_number,
                short_step_id, step_type, status, user_message_id,
                user_message_json, user_request_text, created_at
            ) VALUES (?, ?, ?, 1, 1, 'S-1-USER', 'user_request',
                      'success', ?, ?, ?, ?)
            """,
            (
                "user-step-1",
                payload["action_id"],
                payload["user_id"],
                message.message_id,
                serialize_action_user_message(message),
                render_action_user_request_text(message),
                "2026-08-16T00:10:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, suggestion_id,
                current_job_id, next_event_seq, updated_at
            ) VALUES (?, ?, 'action', 'running', ?, ?, ?, 1, ?)
            """,
            (
                payload["process_id"],
                payload["user_id"],
                payload["action_id"],
                suggestion_id,
                payload["job_id"],
                "2026-08-16T00:20:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, user_id, job_type, process_id, status, logical_key, attempt
            ) VALUES (?, ?, 'execute_action', ?, 'running', ?, 1)
            """,
            (
                payload["job_id"],
                payload["user_id"],
                payload["process_id"],
                payload["action_id"],
            ),
        )
        connection.execute(
            "INSERT INTO job_payloads(job_id, payload_json) VALUES (?, ?)",
            (payload["job_id"], json.dumps(payload)),
        )
        connection.execute(
            "INSERT INTO job_attempts(job_id, attempt_number, status) "
            "VALUES (?, 1, 'running')",
            (payload["job_id"],),
        )


def _insert_recovery_evidence(
    db_path: Path,
    *,
    payload: ActionJobRuntimePayload,
    marker: str = "RECOVERY_REQUEUED",
    event_command_id: str = "message-1",
) -> None:
    event_payload = {
        "kind": "action",
        "process_id": payload["process_id"],
        "action_id": payload["action_id"],
        "command_id": event_command_id,
        "accepted_at": "2026-08-16T00:10:00Z",
        "started_at": STARTED_AT,
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'processing' WHERE action_id = ?",
            (payload["action_id"],),
        )
        connection.execute(
            "UPDATE jobs SET attempt = 2 WHERE job_id = ?",
            (payload["job_id"],),
        )
        connection.execute(
            """
            UPDATE job_attempts
            SET status = 'failed', completed_at = ?, error_code = ?
            WHERE job_id = ? AND attempt_number = 1
            """,
            ("2026-08-16T01:01:00Z", marker, payload["job_id"]),
        )
        connection.execute(
            """
            INSERT INTO job_attempts(job_id, attempt_number, status)
            VALUES (?, 2, 'running')
            """,
            (payload["job_id"],),
        )
        connection.execute(
            """
            INSERT INTO process_events(
                process_id, event_seq, event_id, event_name,
                payload_json, created_at
            ) VALUES (?, 1, ?, 'process_started', ?, ?)
            """,
            (
                payload["process_id"],
                f"{payload['job_id']}-process-started",
                json.dumps(event_payload),
                STARTED_AT,
            ),
        )


def _repository(db_path: Path) -> ActionJobRuntimeRepository:
    return ActionJobRuntimeRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )


def _skip_command(
    payload: ActionJobRuntimePayload,
    *,
    outcome: ActionStartSkipOutcome = "already_processing",
    completed_at: str = "2026-08-16T02:00:00Z",
) -> ActionJobStartSkipCommand:
    return ActionJobStartSkipCommand(
        job_id=payload["job_id"],
        process_id=payload["process_id"],
        user_id=payload["user_id"],
        action_id=payload["action_id"],
        suggestion_id="suggestion-1",
        command_id="message-1",
        accepted_at="2026-08-16T00:30:00Z",
        completed_at=completed_at,
        failure_stage="start_failed",
        outcome=outcome,
    )


@pytest.mark.parametrize(
    ("outcome", "action_status", "expected_error_code"),
    [
        (
            "already_processing",
            "processing",
            "ACTION_START_ALREADY_PROCESSING",
        ),
        ("already_terminal", "success", "ACTION_START_ALREADY_TERMINAL"),
    ],
)
def test_finalize_skipped_action_start_writes_canonical_run_terminal(
    tmp_path: Path,
    outcome: ActionStartSkipOutcome,
    action_status: str,
    expected_error_code: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=_message(suggestion_id="suggestion-1"),
        action_status=action_status,
        suggestion_id="suggestion-1",
    )
    completed_at = "2026-08-16T02:00:00Z"
    canonical_completed_at = "2026-08-16T02:00:00.000000Z"

    command = _skip_command(payload, outcome=outcome, completed_at=completed_at)
    _repository(db_path).finalize_skipped_action_start(command=command)
    _repository(db_path).finalize_skipped_action_start(command=command)

    with sqlite3.connect(db_path) as connection:
        action_row = connection.execute(
            "SELECT status, suggestion_id, updated_at FROM agent_actions"
        ).fetchone()
        job_row = connection.execute(
            "SELECT status, completed_at, heartbeat_at, error_code FROM jobs"
        ).fetchone()
        process_row = connection.execute(
            """
            SELECT status, completed_at, current_job_id, terminal_event_id,
                   updated_at, heartbeat_at
            FROM processes
            """
        ).fetchone()
        attempt_row = connection.execute(
            "SELECT status, completed_at, error_code FROM job_attempts"
        ).fetchone()
        events = connection.execute(
            """
            SELECT event_seq, event_id, event_name, payload_json, created_at
            FROM process_events
            ORDER BY event_seq
            """
        ).fetchall()
        public_event_count = connection.execute(
            "SELECT COUNT(*) FROM agent_process_events"
        ).fetchone()

    terminal_event_id = "job-1-start-skipped-stream-end"
    assert action_row == (
        action_status,
        "suggestion-1",
        "2026-08-16T00:00:00Z",
    )
    assert job_row == (
        "canceled",
        canonical_completed_at,
        canonical_completed_at,
        expected_error_code,
    )
    assert process_row == (
        "canceled",
        canonical_completed_at,
        None,
        terminal_event_id,
        canonical_completed_at,
        canonical_completed_at,
    )
    assert attempt_row == (
        "canceled",
        canonical_completed_at,
        expected_error_code,
    )
    assert [(row[0], row[2], row[4]) for row in events] == [
        (1, "action_start_skipped", canonical_completed_at),
        (2, "stream_end", canonical_completed_at),
    ]
    assert events[1][1] == terminal_event_id
    assert json.loads(events[1][3]) == {
        "kind": "action",
        "process_id": "process-1",
        "suggestion_id": "suggestion-1",
        "action_id": "action-1",
        "command_id": "message-1",
        "status": "canceled",
        "completed_at": canonical_completed_at,
        "failure_code": expected_error_code,
        "failure_stage": "start_failed",
        "failure_message_public": "Action execution was canceled.",
        "physical_run_only": True,
    }
    assert public_event_count == (0,)


def test_finalize_skipped_action_start_rejects_inconsistent_terminal_authority(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=_message(suggestion_id="suggestion-1"),
        action_status="processing",
        suggestion_id="suggestion-1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE processes SET terminal_event_id = 'missing-terminal'"
        )

    with pytest.raises(MigrationError, match="authority is inconsistent"):
        _repository(db_path).finalize_skipped_action_start(
            command=_skip_command(payload)
        )
    with sqlite3.connect(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events"
        ).fetchone()
    assert event_count == (0,)


def _initialize_full_runtime(
    *,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    reset_desktop_session_store()
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.action_messages.read_local_runtime_db_config",
        lambda: (db_path, BUSY_TIMEOUT_MS),
    )
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )


def test_full_runtime_schema_claim_loads_canonical_action_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _initialize_full_runtime(db_path=db_path, monkeypatch=monkeypatch)
    try:
        created = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=NewActionTarget(),
                message=_message(),
            )
        )
        payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-1",
        )
        assert payload is not None

        preparation = _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )

        assert preparation.skip_outcome is None
        assert preparation.context.user_step_id == created.user_step_id
        assert preparation.context.user_message == _message()
        with sqlite3.connect(db_path) as connection:
            status = connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (created.action_id,),
            ).fetchone()[0]
        assert status == "processing"

        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                UPDATE agent_action_steps
                SET runtime_state_checkpoint = '{}',
                    runtime_state_checkpoint_version = 4
                WHERE step_id = ?
                """,
                (created.user_step_id,),
            )
            connection.execute(
                "UPDATE agent_actions SET status = 'success' WHERE action_id = ?",
                (created.action_id,),
            )
            connection.execute(
                "UPDATE jobs SET status = 'completed' WHERE job_id = ?",
                (created.job_id,),
            )
            connection.execute(
                "UPDATE processes SET status = 'completed', current_job_id = NULL "
                "WHERE process_id = ?",
                (created.process_id,),
            )

        followup_message = ActionUserMessageInput(
            message_id="message-2",
            content="Continue the same Action",
            language="ja",
        )
        followup = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=created.action_id,
                    expected_process_id=None,
                ),
                message=followup_message,
            )
        )
        followup_payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-1",
        )
        assert followup_payload is not None

        followup_preparation = _repository(db_path).prepare_execution(
            payload=followup_payload,
            started_at="2026-08-16T02:00:00Z",
        )

        assert followup.action_id == created.action_id
        assert followup.disposition == "started"
        assert followup_preparation.skip_outcome is None
        assert followup_preparation.context.user_step_id == followup.user_step_id
        assert followup_preparation.context.user_step_number == 2
        assert followup_preparation.context.user_step_local_step_number == 2
        assert followup_preparation.context.user_step_short_id == "S-2-USER"
        assert followup_preparation.context.user_message == followup_message
    finally:
        reset_desktop_session_store()


def test_startup_recovery_resumes_prepared_suggestion_action_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _initialize_full_runtime(db_path=db_path, monkeypatch=monkeypatch)
    try:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id, user_id, status, has_suggestion,
                    interaction_contract, answer, suggestion_summary,
                    target_context_json, created_at, updated_at
                ) VALUES (
                    'suggestion-1', 'user-1', 'success', 1, 'action_offer',
                    'Perform the requested work', 'Approved work', '{}', ?, ?
                )
                """,
                ("2026-08-16T00:00:00Z", "2026-08-16T00:00:00Z"),
            )
        created = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=NewActionTarget(suggestion_id="suggestion-1"),
                message=_message(suggestion_id="suggestion-1"),
            )
        )
        payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-before-crash",
        )
        assert payload is not None
        first_preparation = _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )

        repaired_count = repair_inflight_jobs_for_startup(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
        )
        with sqlite3.connect(db_path) as connection:
            action_status_after_repair = connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (created.action_id,),
            ).fetchone()[0]
        recovered_payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-after-crash",
        )
        assert recovered_payload == payload
        second_preparation = _repository(db_path).prepare_execution(
            payload=recovered_payload,
            started_at="2026-08-16T01:30:00Z",
        )

        with sqlite3.connect(db_path) as connection:
            attempts = connection.execute(
                """
                SELECT attempt_number, status, error_code
                FROM job_attempts WHERE job_id = ? ORDER BY attempt_number
                """,
                (created.job_id,),
            ).fetchall()
            internal_start_count = connection.execute(
                """
                SELECT COUNT(*) FROM process_events
                WHERE process_id = ? AND event_name = 'process_started'
                """,
                (created.process_id,),
            ).fetchone()[0]
            public_start_count = connection.execute(
                """
                SELECT COUNT(*) FROM agent_process_events
                WHERE action_id = ? AND event_name = 'process_started'
                """,
                (created.action_id,),
            ).fetchone()[0]
            suggestion_projection = connection.execute(
                """
                SELECT action_status, action_started_at
                FROM agent_suggestions WHERE suggestion_id = 'suggestion-1'
                """
            ).fetchone()
            history_projection = connection.execute(
                """
                SELECT action_status, action_id, last_sequence
                FROM agent_suggestion_history
                WHERE suggestion_id = 'suggestion-1'
                """
            ).fetchone()

        assert first_preparation.skip_outcome is None
        assert repaired_count == 1
        assert action_status_after_repair == "processing"
        assert second_preparation.skip_outcome is None
        assert second_preparation.context == first_preparation.context
        assert attempts == [
            (1, "failed", "RECOVERY_REQUEUED"),
            (2, "running", None),
        ]
        assert internal_start_count == 1
        assert public_start_count == 1
        assert suggestion_projection == ("processing", STARTED_AT)
        assert history_projection == ("processing", created.action_id, 3)
    finally:
        reset_desktop_session_store()


def test_startup_recovery_normally_prepares_action_not_yet_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _initialize_full_runtime(db_path=db_path, monkeypatch=monkeypatch)
    try:
        created = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=NewActionTarget(),
                message=_message(),
            )
        )
        initial_payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-before-prepare",
        )
        assert initial_payload is not None

        repaired_count = repair_inflight_jobs_for_startup(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
        )
        with sqlite3.connect(db_path) as connection:
            action_status_after_repair = connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (created.action_id,),
            ).fetchone()[0]
        recovered_payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="worker-after-prepare-crash",
        )
        assert recovered_payload == initial_payload
        preparation = _repository(db_path).prepare_execution(
            payload=recovered_payload,
            started_at=STARTED_AT,
        )

        with sqlite3.connect(db_path) as connection:
            action_status = connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (created.action_id,),
            ).fetchone()[0]
            process_start_count = connection.execute(
                """
                SELECT COUNT(*) FROM process_events
                WHERE process_id = ? AND event_name = 'process_started'
                """,
                (created.process_id,),
            ).fetchone()[0]
        assert repaired_count == 1
        assert action_status_after_repair == "queued"
        assert preparation.skip_outcome is None
        assert action_status == "processing"
        assert process_start_count == 1
    finally:
        reset_desktop_session_store()


def test_startup_recovery_keeps_tool_approval_action_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _initialize_full_runtime(db_path=db_path, monkeypatch=monkeypatch)
    try:
        created = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=NewActionTarget(),
                message=_message(),
            )
        )
        initial_payload = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="initial-worker",
        )
        assert initial_payload is not None
        _repository(db_path).prepare_execution(
            payload=initial_payload,
            started_at=STARTED_AT,
        )
        finalize_local_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_id=created.job_id,
            succeeded=True,
        )
        checkpoint = json.dumps(
            {
                "pending_approval_request": {
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                }
            }
        )
        approval_payload = build_action_job_payload(
            {
                "job_id": "approval-job-1",
                "process_id": "approval-process-1",
                "action_id": created.action_id,
                "user_id": "user-1",
                "continuation_ref": {
                    "kind": "tool_approval",
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                },
            }
        )
        with sqlite3.connect(db_path) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE agent_action_steps
                    SET runtime_state_checkpoint = ?,
                        runtime_state_checkpoint_version = 1
                    WHERE step_id = ?
                    """,
                    (checkpoint, created.user_step_id),
                )
                connection.execute(
                    """
                    INSERT INTO approval_sessions(
                        approval_session_id, user_id, action_id, tool_id,
                        intent_class, approval_source, status,
                        approved_capabilities_json, command_summary_json,
                        requested_at, decided_at, created_at, tool_request_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "approval-1",
                        "user-1",
                        created.action_id,
                        "tool-1",
                        "read_only",
                        "prompt",
                        "approved_once",
                        '{"required_capabilities":[]}',
                        "{}",
                        "2026-08-16T01:10:00Z",
                        "2026-08-16T01:10:00Z",
                        "2026-08-16T01:10:00Z",
                        "tool-request-1",
                    ),
                )
                # 本番と同じ経路で積む: 承認待ちの Action の identity-only ジョブ。
                enqueue_result = enqueue_local_job(
                    connection=connection,
                    request=build_local_action_enqueue_request(
                        approval_payload,
                        scheduled_at="2026-08-16T01:10:00Z",
                        suggestion_id=None,
                    ),
                )
        assert enqueue_result["inserted_new"] is True
        claimed_approval = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="approval-worker-before-crash",
        )
        assert claimed_approval == approval_payload

        repaired_count = repair_inflight_jobs_for_startup(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
        )
        recovered_approval = claim_next_pending_action_job(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_user_id="user-1",
            claimed_by="approval-worker-after-crash",
        )
        assert recovered_approval == approval_payload
        preparation = _repository(db_path).prepare_execution(
            payload=recovered_approval,
            started_at="2026-08-16T01:30:00Z",
        )

        with sqlite3.connect(db_path) as connection:
            action_status = connection.execute(
                "SELECT status FROM agent_actions WHERE action_id = ?",
                (created.action_id,),
            ).fetchone()[0]
            approval_process_start_count = connection.execute(
                """
                SELECT COUNT(*) FROM process_events
                WHERE process_id = 'approval-process-1'
                  AND event_name = 'process_started'
                """
            ).fetchone()[0]
        assert repaired_count == 1
        assert action_status == "processing"
        assert preparation.skip_outcome is None
        assert preparation.context.checkpoint_step_id == created.user_step_id
        assert preparation.context.approval_session_id == "approval-1"
        assert approval_process_start_count == 0
    finally:
        reset_desktop_session_store()


@pytest.mark.parametrize("has_assistant", [False, True])
def test_prepare_user_step_claims_action_and_restores_typed_input(
    tmp_path: Path,
    has_assistant: bool,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    message = _message()
    _insert_running_envelope(db_path, payload=payload, message=message)

    if has_assistant:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """UPDATE agent_action_steps SET step_number=2, local_step_number=2,
                short_step_id='S-2-USER' WHERE step_id='user-step-1'"""
            )
            connection.execute(
                """INSERT INTO agent_action_steps (
                    step_id,action_id,user_id,step_number,local_step_number,
                    short_step_id,step_type,status,llm_response_text,created_at
                ) VALUES ('assistant-1','action-1','user-1',1,1,'S-1-ASSISTANT',
                    'assistant_message','success','最初の提案','2026-08-16T00:00:00Z')"""
            )

    preparation = _repository(db_path).prepare_execution(
        payload=payload,
        started_at=STARTED_AT,
    )

    assert preparation.skip_outcome is None
    assert preparation.context.suggestion_id is None
    assert preparation.context.user_step_id == "user-step-1"
    assert preparation.context.user_step_created_at == "2026-08-16T00:10:00Z"
    assert preparation.context.user_message == message
    assert preparation.context.user_step_number == (2 if has_assistant else 1)
    assert [
        (entry.step_number, entry.content)
        for entry in preparation.context.preceding_assistant_messages
    ] == ([(1, "最初の提案")] if has_assistant else [])
    assert preparation.context.execution_target.kind == "scratch"
    assert preparation.context.command_id == "message-1"
    assert preparation.context.accepted_at == "2026-08-16T00:10:00Z"
    with sqlite3.connect(db_path) as connection:
        action_row = connection.execute(
            "SELECT status, updated_at FROM agent_actions"
        ).fetchone()
        event_row = connection.execute(
            "SELECT event_name, payload_json FROM process_events"
        ).fetchone()
    assert action_row == ("processing", STARTED_AT)
    assert event_row is not None and event_row[0] == "process_started"
    assert "suggestion_id" not in json.loads(event_row[1])


@pytest.mark.parametrize(
    ("marker", "event_command_id"),
    [
        ("NOT_A_RECOVERY_MARKER", "message-1"),
        ("RECOVERY_REQUEUED", "different-message"),
    ],
)
def test_prepare_recovered_user_step_fails_closed_on_evidence_mismatch(
    tmp_path: Path,
    marker: str,
    event_command_id: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(db_path, payload=payload, message=_message())
    _insert_recovery_evidence(
        db_path,
        payload=payload,
        marker=marker,
        event_command_id=event_command_id,
    )

    preparation = _repository(db_path).prepare_execution(
        payload=payload,
        started_at="2026-08-16T01:30:00Z",
    )

    assert preparation.skip_outcome == "already_processing"
    with sqlite3.connect(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events"
        ).fetchone()[0]
    assert event_count == 1


@pytest.mark.parametrize(
    "marker", ["RECOVERY_REQUEUED", ACTION_JOB_OPERATIONAL_RETRY_ERROR_CODE]
)
def test_prepare_recovered_user_step_survives_dispatch_requeue_attempt(
    tmp_path: Path,
    marker: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(db_path, payload=payload, message=_message())
    _insert_recovery_evidence(db_path, payload=payload, marker=marker)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET attempt = 3 WHERE job_id = ?",
            (payload["job_id"],),
        )
        connection.execute(
            """
            UPDATE job_attempts
            SET status = 'failed', completed_at = ?,
                error_code = 'LOCAL_JOB_DISPATCH_FAILED'
            WHERE job_id = ? AND attempt_number = 2
            """,
            ("2026-08-16T01:20:00Z", payload["job_id"]),
        )
        connection.execute(
            """
            INSERT INTO job_attempts(job_id, attempt_number, status)
            VALUES (?, 3, 'running')
            """,
            (payload["job_id"],),
        )

    preparation = _repository(db_path).prepare_execution(
        payload=payload,
        started_at="2026-08-16T01:30:00Z",
    )

    assert preparation.skip_outcome is None
    with sqlite3.connect(db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events"
        ).fetchone()[0]
    assert event_count == 1


def test_prepare_user_step_rejects_job_logical_key_mismatch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(db_path, payload=payload, message=_message())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET logical_key = 'other-action' WHERE job_id = ?",
            (payload["job_id"],),
        )

    with pytest.raises(MigrationError, match="field=job_logical_key"):
        _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )


def test_prepare_user_step_restores_optional_suggestion_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    message = _message(suggestion_id="suggestion-1")
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=message,
        suggestion_id="suggestion-1",
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.action_job_runtime_repository."
        "project_suggestion_action_started",
        lambda **_kwargs: 1,
    )

    context = (
        _repository(db_path)
        .prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )
        .context
    )

    assert context.suggestion_id == "suggestion-1"
    assert context.accepted_at == "2026-08-16T00:30:00Z"


def test_prepare_followup_user_step_keeps_action_provenance_without_approval_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    message = ActionUserMessageInput(
        message_id="message-2",
        content="Continue with the follow-up",
        language="ja",
    )
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=message,
        suggestion_id="suggestion-1",
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.action_job_runtime_repository."
        "project_suggestion_action_started",
        lambda **_kwargs: 1,
    )

    context = (
        _repository(db_path)
        .prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )
        .context
    )

    assert context.suggestion_id == "suggestion-1"
    assert context.user_message == message
    assert context.accepted_at == "2026-08-16T00:10:00Z"


def test_prepare_user_step_rejects_process_action_binding_mismatch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(db_path, payload=payload, message=_message())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE processes SET action_id = 'other-action' WHERE process_id = ?",
            (payload["process_id"],),
        )

    with pytest.raises(MigrationError, match="envelope was not found"):
        _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )

    with sqlite3.connect(db_path) as connection:
        status = connection.execute("SELECT status FROM agent_actions").fetchone()[0]
    assert status == "queued"


def test_prepare_user_step_rejects_inconsistent_durable_message(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload()
    _insert_running_envelope(db_path, payload=payload, message=_message())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_action_steps SET user_message_id = 'other-message'"
        )

    with pytest.raises(MigrationError, match="message identity is inconsistent"):
        _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )


def test_prepare_tool_approval_loads_checkpoint_context_without_start_claim(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload(
        continuation_ref={
            "kind": "tool_approval",
            "approval_session_id": "approval-1",
            "tool_request_id": "tool-request-1",
        }
    )
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=_message(),
        action_status="processing",
    )
    checkpoint = {
        "pending_approval_request": {
            "approval_session_id": "approval-1",
            "tool_request_id": "tool-request-1",
        }
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, step_type, status,
                runtime_state_checkpoint, created_at
            ) VALUES ('checkpoint-1', 'action-1', 'user-1', 2,
                      'llm_output', 'success', ?, ?)
            """,
            (json.dumps(checkpoint), "2026-08-16T00:15:00Z"),
        )
        connection.execute(
            """
            INSERT INTO approval_sessions(
                approval_session_id, user_id, action_id, tool_request_id, status
            ) VALUES ('approval-1', 'user-1', 'action-1',
                      'tool-request-1', 'approved_once')
            """
        )

    preparation = _repository(db_path).prepare_execution(
        payload=payload,
        started_at=STARTED_AT,
    )

    context = preparation.context
    assert preparation.skip_outcome is None
    assert context.user_step_id == "user-step-1"
    assert context.checkpoint_step_id == "checkpoint-1"
    assert context.approval_session_id == "approval-1"
    assert context.approval_tool_request_id == "tool-request-1"
    with sqlite3.connect(db_path) as connection:
        action_status = connection.execute(
            "SELECT status FROM agent_actions"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events"
        ).fetchone()[0]
    assert action_status == "processing"
    assert event_count == 0


def test_prepare_tool_approval_rejects_checkpoint_request_mismatch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    payload = _payload(
        continuation_ref={
            "kind": "tool_approval",
            "approval_session_id": "approval-1",
            "tool_request_id": "tool-request-1",
        }
    )
    _insert_running_envelope(
        db_path,
        payload=payload,
        message=_message(),
        action_status="processing",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO approval_sessions(
                approval_session_id, user_id, action_id, tool_request_id, status
            ) VALUES ('approval-1', 'user-1', 'action-1',
                      'tool-request-1', 'denied')
            """
        )

    with pytest.raises(MigrationError, match="checkpoint was not found"):
        _repository(db_path).prepare_execution(
            payload=payload,
            started_at=STARTED_AT,
        )
