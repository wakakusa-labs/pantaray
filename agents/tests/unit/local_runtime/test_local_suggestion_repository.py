from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pantaray_agents.action_status import FinalizeActionTerminalCommand
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "sug-1"


class _ActivityRepositoryStub:
    async def get_recent_activity_logs(
        self,
        *,
        user_id: str,
        limit: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(data=[], error=None)

    async def get_recent_activity_summary(
        self,
        *,
        user_id: str,
        summary_type: str,
        limit: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(data=[], error=None)


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
                ) VALUES (?, 'ja', '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z')
                """,
                (USER_ID,),
            )
    return db_path


def _repo(db_path: Path) -> LocalSuggestionRepository:
    return LocalSuggestionRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=_ActivityRepositoryStub(),
    )


def _success_response(*, answer: str) -> SuggestionAgentResponse:
    return SuggestionAgentResponse(
        suggestion_id=SUGGESTION_ID,
        user_id=USER_ID,
        created_at="2026-03-24T00:00:00Z",
        answer=answer,
        thinking="thinking",
        suggestion_summary="summary",
        target_context={
            "organization_name": "Wakakusa",
            "project_name": "Pantaray",
        },
        status=StatusType.SUCCESS,
        has_suggestion=True,
        interaction_contract="action_offer",
    )


def _message_only_response(*, answer: str) -> SuggestionAgentResponse:
    return SuggestionAgentResponse(
        suggestion_id=SUGGESTION_ID,
        user_id=USER_ID,
        created_at="2026-03-24T00:00:00Z",
        answer=answer,
        thinking="thinking",
        status=StatusType.SUCCESS,
        has_suggestion=True,
        interaction_contract="message_only",
    )


@pytest.mark.asyncio
async def test_save_suggestion_run_step_overwrites_processing_with_terminal_state(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)
    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None

    processing = await repo.save_suggestion_run_step(
        suggestion_id=SUGGESTION_ID,
        step_number=2,
        step_kind="tool",
        status="processing",
        tool_name="memory_search",
        tool_call_envelope={
            "tool_id": "memory_search",
            "reason": None,
            "args": {
                "hypothesis": "shared contract mismatch",
                "evidence_goal": "disconfirm",
            },
        },
    )
    assert processing.error is None

    completed = await repo.save_suggestion_run_step(
        suggestion_id=SUGGESTION_ID,
        step_number=2,
        step_kind="tool",
        status="success",
        tool_name="memory_search",
        tool_call_envelope={
            "tool_id": "memory_search",
            "reason": None,
            "args": {
                "hypothesis": "shared contract mismatch",
                "evidence_goal": "disconfirm",
            },
        },
        tool_output={"status": "success", "results": []},
    )

    assert completed.error is None
    assert completed.data is not None
    assert completed.data["status"] == "success"
    assert completed.data["tool_name"] == "memory_search"
    assert "disconfirm" in completed.data["tool_input_json"]
    assert completed.data["tool_output_json"] == '{"status":"success","results":[]}'


@pytest.mark.asyncio
async def test_mark_user_reaction_normalizes_dismiss_to_stored_rejected(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None
    saved = await repo.save_suggestion(
        _success_response(answer="action offer"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )
    assert saved.error is None

    result = await repo.mark_user_reaction(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        user_reaction="dismiss",
        rejected_at="2026-03-24T00:00:01Z",
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["user_reaction"] == "rejected"
    assert result.data["rejected_at"] == "2026-03-24T00:00:01Z"


@pytest.mark.asyncio
async def test_create_processing_suggestion_row_preserves_existing_terminal_row(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None

    saved = await repo.save_suggestion(
        _success_response(answer="first"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )
    assert saved.error is None

    duplicate = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:10:00Z",
    )

    assert duplicate.error is None
    assert duplicate.data is not None
    assert duplicate.data["status"] == "success"
    assert duplicate.data["answer"] == "first"
    assert duplicate.data["created_at"] == "2026-03-24T00:00:00Z"
    assert duplicate.data["has_suggestion"] is True


@pytest.mark.asyncio
async def test_save_suggestion_persists_action_handoff_context(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None

    saved = await repo.save_suggestion(
        _success_response(answer="handoff"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )

    assert saved.error is None
    assert saved.data is not None
    assert saved.data["suggestion_summary"] == "summary"
    assert saved.data["target_context_json"] == {
        "organization_name": "Wakakusa",
        "project_name": "Pantaray",
    }


@pytest.mark.asyncio
async def test_get_suggestion_normalizes_sqlite_boolean_flags(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_name,
                    prompt_version,
                    has_suggestion,
                    interaction_contract,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SUGGESTION_ID,
                    USER_ID,
                    "success",
                    "normalized",
                    "suggestion",
                    "1.0",
                    1,
                    "action_offer",
                    "2026-03-24T00:00:00Z",
                    "2026-03-24T00:00:00Z",
                ),
            )

    result = await repo.get_suggestion(user_id=USER_ID, suggestion_id=SUGGESTION_ID)

    assert result.error is None
    assert result.data is not None
    assert result.data["has_suggestion"] is True


@pytest.mark.asyncio
async def test_save_suggestion_does_not_overwrite_existing_terminal_row(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None

    finalized = await repo.finalize_suggestion_start_error_if_processing(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        error_code="START_FAILED",
        error_message="boom",
    )
    assert finalized.error is None
    assert finalized.data is not None
    assert finalized.data["status"] == "error"

    saved = await repo.save_suggestion(
        _success_response(answer="late-success"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )

    assert saved.error is None
    assert saved.data is not None
    assert saved.data["status"] == "error"
    assert saved.data["answer"] is None
    assert saved.data["error"]["error_code"] == "START_FAILED"


@pytest.mark.asyncio
async def test_message_only_suggestion_rejects_generic_reaction_and_action_updates(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)

    created = await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    assert created.error is None

    saved = await repo.save_suggestion(
        _message_only_response(answer="message only"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )
    assert saved.error is None

    reaction_result = await repo.mark_user_reaction(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        user_reaction="rejected",
        rejected_at="2026-03-24T00:00:01Z",
    )
    status_result = await repo.update_action_status(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        action_status="processing",
    )

    assert (
        reaction_result.error
        == "mark_user_reaction is not allowed for message_only suggestions"
    )
    assert (
        status_result.error
        == "update_action_status is not allowed for message_only suggestions"
    )

    current = await repo.get_suggestion(user_id=USER_ID, suggestion_id=SUGGESTION_ID)
    assert current.error is None
    assert current.data is not None
    assert current.data["interaction_contract"] == "message_only"
    assert current.data["user_reaction"] is None
    assert current.data["action_status"] is None


@pytest.mark.asyncio
async def test_action_terminal_projection_failure_rolls_back_suggestion_update(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)
    await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at="2026-03-24T00:00:00Z",
    )
    await repo.save_suggestion(
        _success_response(answer="action offer"),
        prompt_name="suggestion",
        prompt_version="1.0",
        prompt_text="prompt",
        response_text="response",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id, user_id, suggestion_id, initial_user_message_id,
                status, execution_target_json, final_output,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES ('action-1', ?, ?, 'message-1', 'queued',
                      '{"kind":"scratch"}', '', 'action/executing', '1.0', ?, ?)
            """,
            (USER_ID, SUGGESTION_ID, "2026-03-24T00:00:01Z", "2026-03-24T00:00:01Z"),
        )

    result = await repo.finalize_action_terminal_and_project_history(
        command=FinalizeActionTerminalCommand(
            process_completed_event_id="event-1",
            suggestion_id=SUGGESTION_ID,
            user_id=USER_ID,
            command_id="message-1",
            process_id="process-1",
            action_id="action-1",
            accepted_at="2026-03-24T00:00:01Z",
            completed_at="2026-03-24T00:01:00Z",
            action_status="error",
            failure_code="ACTION_FAILED",
            failure_stage="running_failed",
            failure_message_public="Action failed.",
        )
    )

    assert result.error == "Action is not processing"
    with sqlite3.connect(db_path) as connection:
        suggestion_state = connection.execute(
            """
            SELECT accepted_at, action_status
            FROM agent_suggestions
            WHERE suggestion_id = ?
            """,
            (SUGGESTION_ID,),
        ).fetchone()
        action_status = connection.execute(
            "SELECT status FROM agent_actions WHERE action_id = 'action-1'"
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM agent_process_events"
        ).fetchone()[0]

    assert suggestion_state == (None, None)
    assert action_status == "queued"
    assert event_count == 0
    persisted_state = await repo.get_suggestion_state(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
    )
    assert persisted_state.data is not None
    assert persisted_state.data["action_id"] == "action-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("action_offer", [False, True])
async def test_recent_suggestions_include_the_actual_user_reply_and_reaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_offer: bool,
) -> None:
    from datetime import UTC, datetime

    from pantaray_agents.local_runtime.runtime.action_messages import (
        NewActionTarget,
        SubmitActionMessageCommand,
        submit_action_message,
    )
    from pantaray_agents.local_runtime.runtime.session_store import (
        import_desktop_session,
    )
    from pantaray_agents.schema.agent.action_message import (
        ActionUserMessageInput,
        SuggestionApprovalInput,
    )

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        desktop_access_token="header.payload.signature",
        expires_at="2099-01-01T00:00:00Z",
        session_version="1",
    )
    now = datetime.now(UTC).isoformat()
    repo = _repo(db_path)
    await repo.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=now
    )
    response = (_success_response if action_offer else _message_only_response)(
        answer="Review the estimate?"
    )
    saved = await repo.save_suggestion(
        response, prompt_name="suggestion", prompt_version="react_v2"
    )
    assert saved.error is None
    content = (
        response.answer
        if action_offer
        else "This is already complete; focus on the launch."
    )
    submit_action_message(
        SubmitActionMessageCommand(
            user_id=USER_ID,
            target=(
                NewActionTarget(suggestion_id=SUGGESTION_ID)
                if action_offer
                else NewActionTarget(reply_to_suggestion_id=SUGGESTION_ID)
            ),
            message=ActionUserMessageInput(
                message_id="reply-1",
                content=content,
                suggestion_approval=(
                    SuggestionApprovalInput(
                        suggestion_id=SUGGESTION_ID,
                        approved_at=now,
                        summary="summary",
                        organization_name="Wakakusa",
                        project_name="Pantaray",
                    )
                    if action_offer
                    else None
                ),
                supplement="Prepare a draft only." if action_offer else None,
            ),
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id,ui_language,created_at,updated_at) VALUES ('other','ja',?,?)",
            (now, now),
        )
        connection.execute(
            """INSERT INTO agent_suggestions(suggestion_id,user_id,status,has_suggestion,answer,created_at,updated_at)
               VALUES ('other-suggestion','other','success',1,'Private other-user suggestion',?,?)""",
            (now, now),
        )
    recent = await repo.get_recent_suggestions(USER_ID, limit=5)
    assert recent.error is None
    assert recent.data is not None and len(recent.data) == 1
    entry = recent.data[0]
    assert entry["user_reaction"] == ("accepted" if action_offer else None)
    # An approval's content repeats the Suggestion; only the supplement is the reply.
    assert entry["user_reply"] == ("Prepare a draft only." if action_offer else content)
    assert entry["action_status"] == "queued"
    assert entry["action_result"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("follow_up_process_status", "action_status"),
    [("canceled", "canceled"), ("running", "processing")],
)
async def test_recent_suggestions_keep_the_latest_successful_action_result(
    tmp_path: Path,
    follow_up_process_status: str,
    action_status: str,
) -> None:
    from pantaray_agents.schema.agent.action_message import (
        ActionUserMessageInput,
        SuggestionApprovalInput,
    )

    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)
    now = "2099-01-01T00:00:00Z"
    await repo.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=now
    )
    await repo.save_suggestion(
        _success_response(answer="Check whether PR #1467 is merged?"),
        prompt_name="suggestion",
        prompt_version="react_v2",
    )
    approval = ActionUserMessageInput(
        message_id="message-1",
        content="Check whether PR #1467 is merged?",
        suggestion_approval=SuggestionApprovalInput(
            suggestion_id=SUGGESTION_ID, approved_at=now
        ),
    ).model_dump_json()
    follow_up_terminal = (
        "'event-2'" if follow_up_process_status == "canceled" else "NULL"
    )
    with sqlite3.connect(db_path) as connection:
        # A follow-up turn clears agent_actions.final_output; the first turn's
        # successful terminal is the result the Suggestion must still see.
        connection.executescript(
            f"""
            INSERT INTO agent_actions(action_id,user_id,suggestion_id,initial_user_message_id,
              execution_target_json,status,final_output,prompt_name,prompt_version,created_at,updated_at)
            VALUES ('action-1','{USER_ID}','{SUGGESTION_ID}','message-1','{{"kind":"scratch"}}',
              '{action_status}','','action','1','{now}','{now}');
            INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,updated_at,
              completed_at,heartbeat_at,terminal_event_id,next_event_seq)
            VALUES ('run-1','{USER_ID}','action','completed','action-1','{now}','{now}','{now}','{now}','event-1',2),
              ('run-2','{USER_ID}','action','{follow_up_process_status}','action-1','{now}','{now}',
               NULL,'{now}',{follow_up_terminal},2);
            INSERT INTO process_events(process_id,event_seq,event_id,event_name,payload_json,created_at)
            VALUES ('run-1',1,'event-1','stream_end','{{"final_output":"PR #1467 is already merged."}}','{now}'),
              ('run-2',1,'event-2','stream_end','{{"final_output":null}}','{now}');
            INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,local_step_number,
              short_step_id,step_type,step_name,status,goal_handle,user_message_id,user_message_json,
              user_request_text,accepted_sequence,adopted_process_id,created_at)
            VALUES ('step-1','action-1','{USER_ID}',1,1,'S-1-USER','user_request','user_request',
              'success','S','message-1','{approval}','Check whether PR #1467 is merged?',1,'run-1','{now}'),
              ('step-2','action-1','{USER_ID}',2,2,'S-2-USER','user_request','user_request',
              'success','S',NULL,NULL,'Also check CI',2,'run-2','{now}');
            """
        )

    recent = await repo.get_recent_suggestions(USER_ID)

    assert recent.data is not None
    entry = recent.data[0]
    assert entry["user_reply"] is None
    assert entry["action_status"] == action_status
    assert entry["action_result"] == "PR #1467 is already merged."


@pytest.mark.asyncio
async def test_recent_suggestions_keep_dismissal_distinct_from_no_reply(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)
    await repo.create_processing_suggestion_row(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        created_at=datetime.now(UTC).isoformat(),
    )
    await repo.save_suggestion(
        _success_response(answer="Review again?"),
        prompt_name="suggestion",
        prompt_version="react_v2",
    )
    reaction = await repo.mark_user_reaction(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, user_reaction="dismiss"
    )
    assert reaction.error is None
    recent = await repo.get_recent_suggestions(USER_ID)
    assert recent.data is not None
    assert recent.data[0]["user_reaction"] == "rejected"
    assert recent.data[0]["user_reply"] is None


@pytest.mark.asyncio
async def test_recent_suggestions_carry_the_users_later_instructions_in_the_action(
    tmp_path: Path,
) -> None:
    from pantaray_agents.schema.agent.action_message import (
        ActionUserMessageInput,
        SuggestionApprovalInput,
    )

    db_path = _bootstrap_db(tmp_path)
    repo = _repo(db_path)
    now = "2099-01-01T00:00:00Z"
    await repo.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=now
    )
    await repo.save_suggestion(
        _success_response(answer="Investigate fast-uri and update it?"),
        prompt_name="suggestion",
        prompt_version="react_v2",
    )

    def message(message_id: str, content: str, *, approval: bool = False) -> str:
        return ActionUserMessageInput(
            message_id=message_id,
            content=content,
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id=SUGGESTION_ID, approved_at=now
            )
            if approval
            else None,
        ).model_dump_json()

    rows = [
        (
            1,
            "m-1",
            message("m-1", "Investigate fast-uri and update it?", approval=True),
        ),
        (2, "m-2", message("m-2", "Investigate only, do not update")),
        # The same instruction sent twice is one instruction.
        (3, "m-3", message("m-3", "Investigate only, do not update")),
        (4, "m-4", message("m-4", "Keep going")),
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"""INSERT INTO agent_actions(action_id,user_id,suggestion_id,initial_user_message_id,
              execution_target_json,status,final_output,prompt_name,prompt_version,created_at,updated_at)
            VALUES ('action-1','{USER_ID}','{SUGGESTION_ID}','m-1','{{"kind":"scratch"}}',
              'processing','','action','1','{now}','{now}')"""
        )
        connection.execute(
            f"""INSERT INTO processes(process_id,user_id,kind,status,action_id,started_at,
              updated_at,heartbeat_at,next_event_seq)
            VALUES ('run-1','{USER_ID}','action','running','action-1','{now}','{now}','{now}',1)"""
        )
        for index, (step_number, message_id, payload) in enumerate(rows, start=1):
            connection.execute(
                """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
                  local_step_number,short_step_id,step_type,step_name,status,goal_handle,
                  user_message_id,user_message_json,user_request_text,accepted_sequence,
                  adopted_process_id,created_at)
                VALUES (?, 'action-1', ?, ?, ?, ?, 'user_request', 'user_request',
                  'success', 'S', ?, ?, 'text', ?, 'run-1', ?)""",
                (
                    f"step-{index}",
                    USER_ID,
                    step_number,
                    step_number,
                    f"S-{index}-USER",
                    message_id,
                    payload,
                    index,
                    now,
                ),
            )

    recent = await repo.get_recent_suggestions(USER_ID)

    assert recent.data is not None
    assert recent.data[0]["action_followups"] == [
        "Investigate only, do not update",
        "Keep going",
    ]
