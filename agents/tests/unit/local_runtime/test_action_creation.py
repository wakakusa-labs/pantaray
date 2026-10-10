from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.local_runtime.runtime import (
    action_messages,
    action_suggestion_adapter,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    ActionNotFoundError,
    DeferredActionMessageResult,
    ExistingActionTarget,
    ExpectedProcessConflictError,
    MessageIdentityConflictError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    suggestion_id_for_insight,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.local_runtime.tooling.repository.action_approval_modes import (
    load_action_approval_mode,
    set_action_approval_mode,
)
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.tasks.action_user_message import parse_action_user_message


@pytest.fixture(autouse=True)
def _reset_owner_state() -> None:
    yield
    reset_logged_out_owner()


def _create_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                ui_language TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE agent_suggestions (
                suggestion_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                has_suggestion INTEGER,
                interaction_contract TEXT,
                user_reaction TEXT,
                accepted_at TEXT,
                rejected_at TEXT,
                action_status TEXT,
                action_failure_code TEXT,
                action_failure_stage TEXT,
                action_failure_message_public TEXT,
                action_command_id TEXT,
                action_process_id TEXT,
                action_started_at TEXT,
                answer TEXT,
                suggestion_summary TEXT,
                target_context_json TEXT,
                action_request_payload TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, suggestion_id)
            );
            CREATE TABLE agent_actions (
                action_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                suggestion_id TEXT UNIQUE,
                initial_user_message_id TEXT NOT NULL,
                initial_approval_mode TEXT,
                execution_target_json TEXT NOT NULL,
                status TEXT NOT NULL,
                final_output TEXT NOT NULL,
                error TEXT,
                final_prompt_text TEXT,
                prompt_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, initial_user_message_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(suggestion_id) REFERENCES agent_suggestions(suggestion_id)
            );
            CREATE TABLE action_approval_modes (
                action_id TEXT PRIMARY KEY REFERENCES agent_actions(action_id),
                user_id TEXT NOT NULL REFERENCES users(user_id),
                approval_mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE agent_action_steps (
                step_id TEXT PRIMARY KEY,
                action_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                parent_step_id TEXT,
                step_number INTEGER,
                accepted_sequence INTEGER,
                local_step_number INTEGER,
                short_step_id TEXT,
                step_type TEXT NOT NULL,
                step_name TEXT NOT NULL,
                status TEXT NOT NULL,
                goal_handle TEXT,
                retry_count INTEGER NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                user_message_id TEXT,
                user_message_json TEXT,
                user_request_text TEXT,
                llm_response_text TEXT,
                source_suggestion_id TEXT,
                adoption_canceled_at TEXT,
                expected_process_id TEXT,
                adopted_process_id TEXT,
                runtime_state_checkpoint TEXT,
                runtime_state_checkpoint_version INTEGER,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, user_message_id),
                FOREIGN KEY(action_id) REFERENCES agent_actions(action_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE UNIQUE INDEX uq_agent_action_steps_reply_source
                ON agent_action_steps(user_id, source_suggestion_id)
                WHERE source_suggestion_id IS NOT NULL;
            CREATE TABLE processes (
                process_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                suggestion_id TEXT,
                action_id TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                heartbeat_at TEXT NOT NULL,
                terminal_event_id TEXT,
                acknowledged_at TEXT,
                current_job_id TEXT,
                next_event_seq INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE process_events (
                process_event_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                process_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                chunk_index INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(process_id, event_seq),
                UNIQUE(process_id, event_id),
                FOREIGN KEY(process_id) REFERENCES processes(process_id)
            );
            CREATE TABLE agent_process_events (
                event_id TEXT PRIMARY KEY,
                suggestion_id TEXT,
                user_id TEXT NOT NULL,
                action_id TEXT,
                sequence INTEGER NOT NULL,
                event_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(suggestion_id, sequence),
                FOREIGN KEY(suggestion_id) REFERENCES agent_suggestions(suggestion_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(action_id) REFERENCES agent_actions(action_id)
            );
            CREATE TABLE agent_suggestion_history (
                suggestion_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                suggestion_created_at TEXT NOT NULL,
                suggestion_updated_at TEXT NOT NULL,
                suggestion_status TEXT NOT NULL,
                has_suggestion INTEGER NOT NULL,
                answer TEXT NOT NULL,
                interaction_contract TEXT,
                user_reaction TEXT,
                accepted_at TEXT,
                rejected_at TEXT,
                action_status TEXT,
                action_failure_code TEXT,
                action_failure_stage TEXT,
                action_failure_message_public TEXT,
                action_request_payload_present INTEGER NOT NULL,
                action_id TEXT,
                action_created_at TEXT,
                action_updated_at TEXT,
                final_output TEXT,
                last_sequence INTEGER NOT NULL,
                FOREIGN KEY(suggestion_id) REFERENCES agent_suggestions(suggestion_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(action_id) REFERENCES agent_actions(action_id)
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                job_type TEXT NOT NULL,
                process_id TEXT,
                status TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                logical_key TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(process_id) REFERENCES processes(process_id)
            );
            CREATE TABLE job_payloads (
                job_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(job_id)
            );
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('user-1', 'ja', '2026-08-16T00:00:00Z', '2026-08-16T00:00:00Z');
            """
        )


@pytest.fixture
def creation_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "runtime.db"
    _create_schema(db_path)
    # Submitting as the logged-out owner exercises the real owner check.
    register_logged_out_owner("user-1")
    monkeypatch.setattr(
        action_messages,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(
        action_messages,
        "now_utc_iso",
        lambda: "2026-08-16T01:02:03.000Z",
    )
    return db_path


def _text_command(
    *, text: str = "Do the work", approval_mode: ApprovalMode | None = None
) -> SubmitActionMessageCommand:
    return SubmitActionMessageCommand(
        user_id="user-1",
        target=NewActionTarget(approval_mode=approval_mode),
        message=ActionUserMessageInput(
            message_id="message-1",
            content=text,
            language="ja",
        ),
    )


def _insert_reply_source(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_suggestions(
                suggestion_id,user_id,status,has_suggestion,interaction_contract,
                answer,created_at,updated_at)
            VALUES ('comment-1','user-1','success',1,'message_only',
                    '元の発言','2026-08-16T01:00:00.000Z','2026-08-16T01:00:00.000Z')"""
        )


def _reply_command() -> SubmitActionMessageCommand:
    return _text_command(approval_mode="always_allow").model_copy(
        update={
            "target": NewActionTarget(
                approval_mode="always_allow", reply_to_suggestion_id="comment-1"
            )
        }
    )


def test_reply_snapshots_assistant_before_user_and_replays_without_reading_source(
    creation_db: Path,
) -> None:
    _insert_reply_source(creation_db)
    command = _reply_command()
    result = submit_action_message(command)
    with sqlite3.connect(creation_db) as connection:
        rows = connection.execute(
            """SELECT step_type,step_number,short_step_id,llm_response_text,
                      user_request_text,source_suggestion_id,adopted_process_id
               FROM agent_action_steps ORDER BY step_number"""
        ).fetchall()
        assert rows == [
            (
                "assistant_message",
                1,
                "S-1-ASSISTANT",
                "元の発言",
                None,
                "comment-1",
                result.process_id,
            ),
            (
                "user_request",
                2,
                "S-2-USER",
                None,
                "Do the work",
                None,
                result.process_id,
            ),
        ]
        assert connection.execute(
            "SELECT suggestion_id FROM agent_actions"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT user_reaction,action_status FROM agent_suggestions"
        ).fetchone() == (None, None)
        connection.execute("DELETE FROM agent_suggestions")
    assert submit_action_message(command) == replace(result, inserted=False)
    with pytest.raises(MessageIdentityConflictError, match="reply origin"):
        submit_action_message(
            command.model_copy(
                update={"target": NewActionTarget(approval_mode="always_allow")}
            )
        )
    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "invalid_source",
    [
        "DELETE FROM agent_suggestions",
        "UPDATE agent_suggestions SET user_id='other-user'",
        "UPDATE agent_suggestions SET status='processing'",
        "UPDATE agent_suggestions SET has_suggestion=0",
        "UPDATE agent_suggestions SET interaction_contract='action_offer'",
        """UPDATE agent_suggestions
           SET interaction_contract='action_offer',user_reaction='accepted'""",
        "UPDATE agent_suggestions SET answer='   '",
    ],
)
def test_reply_rejects_unavailable_source_atomically(
    creation_db: Path,
    invalid_source: str,
) -> None:
    _insert_reply_source(creation_db)
    with sqlite3.connect(creation_db) as connection:
        connection.execute(invalid_source)
    with pytest.raises(ActionMessageConflictError, match="not available for reply"):
        submit_action_message(_reply_command())
    with sqlite3.connect(creation_db) as connection:
        for table in ("agent_actions", "agent_action_steps", "processes", "jobs"):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )


def test_reply_to_a_dismissed_offer_starts_an_action_and_keeps_the_dismissal(
    creation_db: Path,
) -> None:
    _insert_reply_source(creation_db)
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """UPDATE agent_suggestions
               SET interaction_contract='action_offer',user_reaction='rejected',
                   rejected_at='2026-08-16T01:01:00.000Z'"""
        )
    result = submit_action_message(_reply_command())
    assert isinstance(result, StartedActionMessageResult)
    with sqlite3.connect(creation_db) as connection:
        assert connection.execute(
            """SELECT step_type,source_suggestion_id,llm_response_text,adopted_process_id
               FROM agent_action_steps WHERE step_number=1"""
        ).fetchone() == (
            "assistant_message",
            "comment-1",
            "元の発言",
            result.process_id,
        )
        assert connection.execute(
            "SELECT suggestion_id FROM agent_actions"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id=?", (result.job_id,)
        ).fetchone() == ("queued",)
        assert connection.execute(
            """SELECT user_reaction,rejected_at,accepted_at,action_status,
                      action_process_id
               FROM agent_suggestions"""
        ).fetchone() == ("rejected", "2026-08-16T01:01:00.000Z", None, None, None)


def test_reply_cannot_also_approve_a_suggestion() -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        NewActionTarget(suggestion_id="offer-1", reply_to_suggestion_id="comment-1")


def test_create_action_persists_one_atomic_creation_envelope(
    creation_db: Path,
) -> None:
    result = submit_action_message(_text_command())

    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        action_row = connection.execute("SELECT * FROM agent_actions").fetchone()
        step_row = connection.execute("SELECT * FROM agent_action_steps").fetchone()
        process_row = connection.execute("SELECT * FROM processes").fetchone()
        job_row = connection.execute("SELECT * FROM jobs").fetchone()
        payload_row = connection.execute("SELECT * FROM job_payloads").fetchone()
        accepted_event_row = connection.execute(
            "SELECT * FROM agent_process_events "
            "WHERE event_name = 'action_message_accepted'"
        ).fetchone()
        adopted_event_row = connection.execute(
            "SELECT * FROM agent_process_events "
            "WHERE event_name = 'action_message_adopted'"
        ).fetchone()
        old_event_count = connection.execute(
            "SELECT COUNT(*) FROM process_events WHERE event_name = 'action_requested'"
        ).fetchone()[0]

    assert result.inserted is True
    assert result.disposition == "started"
    assert result.action_status == "queued"
    assert result.message_id == "message-1"
    assert action_row is not None
    assert action_row["status"] == "queued"
    assert action_row["initial_user_message_id"] == "message-1"
    assert json.loads(action_row["execution_target_json"]) == {"kind": "scratch"}
    assert step_row is not None
    assert step_row["step_id"] == result.user_step_id
    assert step_row["accepted_sequence"] == 1
    assert step_row["adopted_process_id"] == result.process_id
    assert step_row["short_step_id"] == "S-1-USER"
    assert step_row["user_request_text"] == "Do the work"
    assert parse_action_user_message(step_row["user_message_json"]).language == "ja"
    assert process_row is not None and process_row["process_id"] == result.process_id
    assert job_row is not None and job_row["job_id"] == result.job_id
    assert payload_row is not None
    assert json.loads(payload_row["payload_json"]) == {
        "job_id": result.job_id,
        "process_id": result.process_id,
        "action_id": result.action_id,
        "user_id": "user-1",
        "continuation_ref": {
            "kind": "user_step",
            "user_step_id": result.user_step_id,
        },
    }
    assert accepted_event_row is not None
    assert accepted_event_row["suggestion_id"] is None
    assert accepted_event_row["action_id"] == result.action_id
    assert accepted_event_row["sequence"] == step_row["accepted_sequence"]
    assert json.loads(accepted_event_row["payload"]) == {
        "data": {
            "action_id": result.action_id,
            "message_id": "message-1",
            "step_id": result.user_step_id,
        }
    }
    assert adopted_event_row is not None
    assert adopted_event_row["sequence"] == accepted_event_row["sequence"] + 1
    assert json.loads(adopted_event_row["payload"])["data"] == {
        "action_id": result.action_id,
        "message_id": "message-1",
        "step_id": result.user_step_id,
        "process_id": result.process_id,
    }
    assert old_event_count == 0


def test_create_action_replays_same_message_identity_without_duplicate_rows(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    replay = submit_action_message(_text_command())

    assert replay == StartedActionMessageResult(
        disposition="started",
        action_id=first.action_id,
        message_id="message-1",
        user_step_id=first.user_step_id,
        action_status="queued",
        process_id=first.process_id,
        job_id=first.job_id,
        inserted=False,
    )
    assert submit_action_message(_text_command()) == replay
    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_process_events "
                "WHERE event_name IN ('action_message_accepted', "
                "'action_message_adopted')"
            ).fetchone()[0]
            == 2
        )


def test_active_submit_persists_pending_fence_and_replays_without_mutation(
    creation_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter(("2026-08-16T01:02:03.000Z", "2026-08-16T01:02:04.000Z"))
    monkeypatch.setattr(
        action_messages,
        "now_utc_iso",
        lambda: next(timestamps, "2026-08-16T01:02:05.000Z"),
    )
    first = submit_action_message(_text_command())
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=first.process_id,
        ),
        message=ActionUserMessageInput(message_id="message-2", content="More work"),
    )

    pending = submit_action_message(command)
    assert pending == DeferredActionMessageResult(
        disposition="pending",
        action_id=first.action_id,
        message_id="message-2",
        user_step_id=pending.user_step_id,
        action_status="queued",
        process_id=None,
        job_id=None,
        inserted=True,
    )
    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM agent_action_steps WHERE step_id=?",
            (pending.user_step_id,),
        ).fetchone()
        assert row is not None
        assert row["accepted_sequence"] == 3
        assert row["expected_process_id"] == first.process_id
        assert row["step_number"] is None
        assert row["local_step_number"] is None
        assert row["short_step_id"] is None
        assert row["adopted_process_id"] is None
        assert row["adoption_canceled_at"] is None
        assert row["created_at"] == "2026-08-16T01:02:04.000Z"
        action_updated_at = connection.execute(
            "SELECT updated_at FROM agent_actions WHERE action_id=?",
            (first.action_id,),
        ).fetchone()[0]
        counts_before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "agent_action_steps",
                "processes",
                "jobs",
                "agent_process_events",
            )
        )

    assert submit_action_message(command) == DeferredActionMessageResult(
        disposition="pending",
        action_id=first.action_id,
        message_id="message-2",
        user_step_id=pending.user_step_id,
        action_status="queued",
        process_id=None,
        job_id=None,
        inserted=False,
    )
    with pytest.raises(MessageIdentityConflictError, match="expected process"):
        submit_action_message(
            command.model_copy(
                update={
                    "target": ExistingActionTarget(
                        action_id=first.action_id,
                        expected_process_id=None,
                    )
                }
            )
        )
    with sqlite3.connect(creation_db) as connection:
        counts_after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "agent_action_steps",
                "processes",
                "jobs",
                "agent_process_events",
            )
        )
        replayed_updated_at = connection.execute(
            "SELECT updated_at FROM agent_actions WHERE action_id=?",
            (first.action_id,),
        ).fetchone()[0]
    assert action_updated_at == replayed_updated_at == "2026-08-16T01:02:04.000Z"
    assert counts_after == counts_before

    with pytest.raises(ExpectedProcessConflictError):
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=first.action_id,
                    expected_process_id=None,
                ),
                message=ActionUserMessageInput(
                    message_id="message-3", content="Do not steer implicitly"
                ),
            )
        )


def test_create_action_rejects_message_identity_with_different_content(
    creation_db: Path,
) -> None:
    submit_action_message(_text_command())

    with pytest.raises(MessageIdentityConflictError) as captured:
        submit_action_message(_text_command(text="Different work"))
    assert captured.value.failure_type == "MessageIdentityConflict"


def test_submit_action_message_raises_typed_not_found_without_mutation(
    creation_db: Path,
) -> None:
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id="missing-action",
            expected_process_id=None,
        ),
        message=ActionUserMessageInput(message_id="message-1", content="Continue"),
    )

    with pytest.raises(ActionNotFoundError) as captured:
        submit_action_message(command)

    assert captured.value.failure_type == "ActionNotFound"
    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_an_insight_sourced_suggestion_still_requires_typed_user_approval(
    creation_db: Path,
) -> None:
    suggestion_id = suggestion_id_for_insight("insight-1")
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, has_suggestion,
                interaction_contract, answer, suggestion_summary,
                target_context_json, created_at, updated_at
            ) VALUES (?, ?, 'success', 1, 'action_offer', ?, ?, NULL, ?, ?)
            """,
            (
                suggestion_id,
                "user-1",
                "Apply the approved change",
                "Keep the edit narrow",
                "2026-08-16T00:00:00Z",
                "2026-08-16T00:00:00Z",
            ),
        )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=NewActionTarget(suggestion_id=suggestion_id),
        message=ActionUserMessageInput(
            message_id="message-unapproved-1",
            content="Apply the approved change",
        ),
    )

    with pytest.raises(ActionMessageConflictError):
        submit_action_message(command)

    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT user_reaction FROM agent_suggestions"
            ).fetchone()[0]
            is None
        )


def test_create_action_projects_typed_suggestion_approval_in_same_transaction(
    creation_db: Path,
) -> None:
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, has_suggestion,
                interaction_contract, answer, suggestion_summary,
                target_context_json, created_at, updated_at
            ) VALUES (?, ?, 'success', 1, 'action_offer', ?, ?, ?, ?, ?)
            """,
            (
                "suggestion-1",
                "user-1",
                "Apply the approved change",
                "Keep the edit narrow",
                json.dumps(
                    {
                        "organization_name": "Wakakusa",
                        "project_name": "Pantaray",
                    }
                ),
                "2026-08-16T00:00:00Z",
                "2026-08-16T00:00:00Z",
            ),
        )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=NewActionTarget(suggestion_id="suggestion-1"),
        message=ActionUserMessageInput(
            message_id="message-approval-1",
            content="Apply the approved change",
            supplement="Run the focused regression before finishing.",
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id="suggestion-1",
                approved_at="2026-08-16T01:02:03.000Z",
                summary="Keep the edit narrow",
                organization_name="Wakakusa",
                project_name="Pantaray",
            ),
        ),
    )

    result = submit_action_message(command)

    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        suggestion_row = connection.execute(
            "SELECT * FROM agent_suggestions"
        ).fetchone()
        action_suggestion_id = connection.execute(
            "SELECT suggestion_id FROM agent_actions"
        ).fetchone()[0]
        history_row = connection.execute(
            "SELECT action_id, action_status FROM agent_suggestion_history"
        ).fetchone()
        suggestion_event_row = connection.execute(
            "SELECT suggestion_id, action_id, event_name, payload "
            "FROM agent_process_events WHERE suggestion_id = 'suggestion-1'"
        ).fetchone()
        accepted_event_row = connection.execute(
            "SELECT suggestion_id, action_id, event_name, payload "
            "FROM agent_process_events WHERE action_id = ?",
            (result.action_id,),
        ).fetchone()
        message_json = connection.execute(
            "SELECT user_message_json FROM agent_action_steps WHERE step_id = ?",
            (result.user_step_id,),
        ).fetchone()[0]
        suggestion_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(agent_suggestions)")
        }
    assert result.inserted is True
    assert suggestion_row is not None
    assert suggestion_row["user_reaction"] == "accepted"
    assert suggestion_row["accepted_at"] == "2026-08-16T01:02:03.000Z"
    assert suggestion_row["action_status"] == "idle"
    assert suggestion_row["action_command_id"] == "message-approval-1"
    assert suggestion_row["action_process_id"] == result.process_id
    assert suggestion_row["action_started_at"] is None
    assert suggestion_row["action_failure_code"] is None
    assert suggestion_row["action_failure_stage"] is None
    assert suggestion_row["action_failure_message_public"] is None
    assert suggestion_row["action_request_payload"] is None
    assert "action_execution_id" not in suggestion_columns
    assert action_suggestion_id == "suggestion-1"
    assert history_row is not None
    assert history_row["action_id"] == result.action_id
    assert history_row["action_status"] == "idle"
    assert suggestion_event_row is not None
    assert suggestion_event_row["suggestion_id"] == "suggestion-1"
    assert suggestion_event_row["action_id"] is None
    assert suggestion_event_row["event_name"] == "suggestion_reaction_committed"
    assert json.loads(suggestion_event_row["payload"]) == {
        "data": {
            "reaction": "accepted",
            "committed_at": "2026-08-16T01:02:03.000Z",
        },
        "meta": {"suggestion_id": "suggestion-1"},
    }
    assert accepted_event_row is not None
    assert accepted_event_row["suggestion_id"] is None
    assert accepted_event_row["action_id"] == result.action_id
    assert accepted_event_row["event_name"] == "action_message_accepted"
    assert parse_action_user_message(message_json).supplement == (
        "Run the focused regression before finishing."
    )
    assert submit_action_message(command).inserted is False
    with pytest.raises(MessageIdentityConflictError):
        submit_action_message(
            command.model_copy(
                update={
                    "message": command.message.model_copy(
                        update={"supplement": "Use a different condition."}
                    )
                }
            )
        )


def test_suggestion_action_claim_projects_processing_state_in_same_transaction(
    creation_db: Path,
) -> None:
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, has_suggestion,
                interaction_contract, answer, target_context_json,
                created_at, updated_at
            ) VALUES (
                'suggestion-1', 'user-1', 'success', 1, 'action_offer',
                'Do the work', '{}',
                '2026-08-16T00:00:00Z', '2026-08-16T00:00:00Z'
            )
            """
        )
    created = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(suggestion_id="suggestion-1"),
            message=ActionUserMessageInput(
                message_id="message-1",
                content="Do the work",
                suggestion_approval=SuggestionApprovalInput(
                    suggestion_id="suggestion-1",
                    approved_at="2026-08-16T01:02:03.000Z",
                ),
            ),
        )
    )
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running' WHERE job_id = ?",
            (created.job_id,),
        )
        connection.execute(
            """
            UPDATE processes
            SET status = 'running', current_job_id = ?
            WHERE process_id = ?
            """,
            (created.job_id, created.process_id),
        )
        payload_json = connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id = ?",
            (created.job_id,),
        ).fetchone()[0]

    preparation = ActionJobRuntimeRepository(
        db_path=creation_db,
        busy_timeout_ms=1_000,
    ).prepare_execution(
        payload=parse_action_job_payload_json(payload_json),
        started_at="2026-08-16T01:02:04.000Z",
    )

    assert preparation.skip_outcome is None
    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        action_row = connection.execute(
            "SELECT status FROM agent_actions WHERE action_id = ?",
            (created.action_id,),
        ).fetchone()
        suggestion_row = connection.execute(
            """
            SELECT action_status, action_command_id, action_process_id,
                   action_started_at
            FROM agent_suggestions
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()
        history_row = connection.execute(
            """
            SELECT action_status, action_id, last_sequence
            FROM agent_suggestion_history
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()
        public_event = connection.execute(
            """
            SELECT sequence, payload
            FROM agent_process_events
            WHERE suggestion_id = 'suggestion-1'
              AND event_name = 'process_started'
            """
        ).fetchone()
        internal_event = connection.execute(
            """
            SELECT payload_json
            FROM process_events
            WHERE process_id = ? AND event_name = 'process_started'
            """,
            (created.process_id,),
        ).fetchone()

    assert action_row is not None and action_row["status"] == "processing"
    assert suggestion_row is not None
    assert dict(suggestion_row) == {
        "action_status": "processing",
        "action_command_id": "message-1",
        "action_process_id": created.process_id,
        "action_started_at": "2026-08-16T01:02:04.000Z",
    }
    assert history_row is not None
    assert dict(history_row) == {
        "action_status": "processing",
        "action_id": created.action_id,
        "last_sequence": 3,
    }
    assert public_event is not None
    public_payload = json.loads(public_event["payload"])
    assert public_payload["data"]["action_id"] == created.action_id
    assert public_payload["data"]["process_id"] == created.process_id
    assert internal_event is not None
    assert json.loads(internal_event["payload_json"])["persisted_sequence"] == int(
        public_event["sequence"]
    )


def test_create_action_rolls_back_when_suggestion_projection_fails(
    creation_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, has_suggestion,
                interaction_contract, answer, target_context_json,
                created_at, updated_at
            ) VALUES (
                'suggestion-1', 'user-1', 'success', 1, 'action_offer',
                'Do the work', '{}',
                '2026-08-16T00:00:00Z', '2026-08-16T00:00:00Z'
            )
            """
        )
    monkeypatch.setattr(
        action_suggestion_adapter,
        "rebuild_history_projection",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("projection failed")),
    )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=NewActionTarget(suggestion_id="suggestion-1"),
        message=ActionUserMessageInput(
            message_id="message-1",
            content="Do the work",
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id="suggestion-1",
                approved_at="2026-08-16T01:02:03.000Z",
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        submit_action_message(command)

    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_process_events").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT user_reaction FROM agent_suggestions"
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("reply", [False, True])
def test_create_action_rolls_back_when_acceptance_event_append_fails(
    creation_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: bool,
) -> None:
    if reply:
        _insert_reply_source(creation_db)
    monkeypatch.setattr(
        action_messages,
        "append_action_invalidation_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("append failed")),
    )

    with pytest.raises(RuntimeError, match="append failed"):
        submit_action_message(
            _reply_command() if reply else _text_command(approval_mode="always_allow")
        )

    with sqlite3.connect(creation_db) as connection:
        for table in (
            "agent_actions",
            "action_approval_modes",
            "agent_action_steps",
            "processes",
            "jobs",
            "agent_process_events",
        ):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )


def _finish_initial_run(db_path: Path, *, action_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint = '{}',
                runtime_state_checkpoint_version = 4
            WHERE step_id = (
                SELECT step_id
                FROM agent_action_steps
                WHERE action_id = ?
                ORDER BY step_number DESC, created_at DESC, step_id DESC
                LIMIT 1
            )
            """,
            (action_id,),
        )
        connection.execute(
            "UPDATE agent_actions SET status = 'success', final_output = 'done' "
            "WHERE action_id = ?",
            (action_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE logical_key = ?",
            (action_id,),
        )
        connection.execute(
            "UPDATE processes SET status = 'completed', completed_at = updated_at "
            "WHERE action_id = ?",
            (action_id,),
        )


def _fail_action_run(
    db_path: Path,
    result: StartedActionMessageResult,
    *,
    stage: str | None,
) -> None:
    error_details = {"exception_type": "RuntimeError"}
    if stage is not None:
        error_details["stage"] = stage
    error = json.dumps(
        {
            "error_type": "internal_error",
            "error_code": "ACTION_RUNTIME_PREPARE_FAILED",
            "error_message": "Action processing encountered an internal error.",
            "error_details": error_details,
            "severity": "error",
            "metadata": {
                "action_id": result.action_id,
                "suggestion_id": None,
                "user_id": "user-1",
            },
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'error', error = ? WHERE action_id = ?",
            (error, result.action_id),
        )
        connection.execute(
            "UPDATE jobs SET status = 'failed' WHERE job_id = ?", (result.job_id,)
        )
        connection.execute(
            "UPDATE processes SET status = 'failed', completed_at = updated_at "
            "WHERE process_id = ?",
            (result.process_id,),
        )


def _followup_command(
    failed: StartedActionMessageResult,
    *,
    message_id: str,
) -> SubmitActionMessageCommand:
    return SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=failed.action_id,
            expected_process_id=failed.process_id,
        ),
        message=ActionUserMessageInput(message_id=message_id, content="Retry"),
    )


@pytest.mark.parametrize("accepted_sequence", [None, 0])
def test_action_process_lineage_requires_positive_root_accepted_sequence(
    creation_db: Path, accepted_sequence: int | None
) -> None:
    created = submit_action_message(_text_command())
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "UPDATE agent_action_steps SET accepted_sequence=? WHERE step_number=1",
            (accepted_sequence,),
        )
        connection.row_factory = sqlite3.Row
        with pytest.raises(MigrationError, match="accepted_sequence must be positive"):
            resolve_action_process_lineage_in_connection(
                connection=connection,
                user_id="user-1",
                action_id=created.action_id,
                process_id=created.process_id,
            )


def test_action_process_lineage_rejects_a_process_with_two_owning_jobs(
    creation_db: Path,
) -> None:
    created = submit_action_message(_text_command())
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at,"
            "logical_key) VALUES ('duplicate-job','user-1','execute_action',?,"
            "'completed','now',?)",
            (created.process_id, created.action_id),
        )
        connection.commit()
    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(MigrationError, match="exactly one owning job"):
            resolve_action_process_lineage_in_connection(
                connection=connection,
                user_id="user-1",
                action_id=created.action_id,
                process_id=created.process_id,
            )
        assert connection.total_changes == 0


def test_submit_action_message_appends_followup_to_same_action(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    _finish_initial_run(creation_db, action_id=first.action_id)
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, parent_step_id, step_number,
                local_step_number, short_step_id, step_type, step_name, status,
                goal_handle, retry_count, prompt_tokens, completion_tokens,
                started_at, completed_at, created_at
            ) VALUES (
                'think-2', ?, 'user-1', ?, 2, 2, 'S-2-THINK',
                'llm_output', 'planning', 'success', 'S', 0, 1, 1,
                '2026-08-16T01:03:00Z', '2026-08-16T01:03:01Z',
                '2026-08-16T01:03:00Z'
            )
            """,
            (first.action_id, first.user_step_id),
        )
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, parent_step_id, step_number,
                local_step_number, short_step_id, step_type, step_name, status,
                goal_handle, retry_count, prompt_tokens, completion_tokens,
                started_at, completed_at, created_at
            ) VALUES (
                'tool-2', ?, 'user-1', 'think-2', 2, 2, 'S-2-TOOL',
                'tool_execution', 'submit_final_answer', 'success', 'S', 0, 0, 0,
                '2026-08-16T01:03:02Z', '2026-08-16T01:03:03Z',
                '2026-08-16T01:03:02Z'
            )
            """,
            (first.action_id,),
        )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=None,
        ),
        message=ActionUserMessageInput(
            message_id="message-2",
            content="Check the result again",
            language="ja",
        ),
    )
    second = submit_action_message(command)

    assert second.action_id == first.action_id
    assert second.disposition == "started"
    assert second.inserted is True
    with sqlite3.connect(creation_db) as connection:
        connection.row_factory = sqlite3.Row
        action = connection.execute(
            "SELECT * FROM agent_actions WHERE action_id = ?", (first.action_id,)
        ).fetchone()
        step = connection.execute(
            "SELECT * FROM agent_action_steps WHERE step_id = ?",
            (second.user_step_id,),
        ).fetchone()
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id = ?",
                (second.job_id,),
            ).fetchone()[0]
        )
        action_count = connection.execute(
            "SELECT COUNT(*) FROM agent_actions"
        ).fetchone()[0]
        accepted_event_sequence = connection.execute(
            "SELECT sequence FROM agent_process_events "
            "WHERE action_id = ? AND event_name = 'action_message_accepted' "
            "ORDER BY sequence DESC LIMIT 1",
            (first.action_id,),
        ).fetchone()[0]
        adopted_event_sequence = connection.execute(
            "SELECT sequence FROM agent_process_events "
            "WHERE action_id = ? AND event_name = 'action_message_adopted' "
            "ORDER BY sequence DESC LIMIT 1",
            (first.action_id,),
        ).fetchone()[0]
    assert action is not None
    assert action["status"] == "queued"
    assert action["final_output"] == ""
    assert action_count == 1
    assert step is not None
    assert step["accepted_sequence"] == 3
    assert accepted_event_sequence == step["accepted_sequence"]
    assert step["step_number"] == 3
    assert step["local_step_number"] == 3
    assert step["short_step_id"] == "S-3-USER"
    assert step["parent_step_id"] == "tool-2"
    assert step["adopted_process_id"] == second.process_id
    assert step["user_request_text"] == "Check the result again"
    assert adopted_event_sequence == accepted_event_sequence + 1
    assert payload["continuation_ref"] == {
        "kind": "user_step",
        "user_step_id": second.user_step_id,
    }

    replay = submit_action_message(command)
    assert replay == StartedActionMessageResult(
        disposition="started",
        action_id=second.action_id,
        message_id="message-2",
        user_step_id=second.user_step_id,
        action_status="queued",
        process_id=second.process_id,
        job_id=second.job_id,
        inserted=False,
    )


def test_submit_action_message_accepts_root_captured_before_terminal_commit(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=first.process_id,
        ),
        message=ActionUserMessageInput(message_id="message-2", content="Continue"),
    )
    _finish_initial_run(creation_db, action_id=first.action_id)

    followup = submit_action_message(command)

    assert followup.action_id == first.action_id
    assert followup.disposition == "started"
    with sqlite3.connect(creation_db) as connection:
        adopted_process_id = connection.execute(
            "SELECT adopted_process_id FROM agent_action_steps WHERE step_id=?",
            (followup.user_step_id,),
        ).fetchone()[0]
        event_order = [
            row[0]
            for row in connection.execute(
                "SELECT event_name FROM agent_process_events WHERE action_id=? "
                "AND json_extract(payload,'$.data.message_id')='message-2' "
                "ORDER BY sequence",
                (first.action_id,),
            )
        ]
    assert adopted_process_id == followup.process_id
    assert event_order == ["action_message_accepted", "action_message_adopted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_older_checkpoint", [False, True])
async def test_submit_action_message_recovers_exact_prepare_failure(
    creation_db: Path,
    has_older_checkpoint: bool,
) -> None:
    first = submit_action_message(_text_command())
    failed = first
    if has_older_checkpoint:
        _finish_initial_run(creation_db, action_id=first.action_id)
        failed = submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=first.action_id,
                    expected_process_id=first.process_id,
                ),
                message=ActionUserMessageInput(
                    message_id="message-2", content="Continue"
                ),
            )
        )
    _fail_action_run(creation_db, failed, stage="prepare")

    resumed = submit_action_message(_followup_command(failed, message_id="message-3"))

    assert resumed.disposition == "started"
    resume_result = await LocalActionRepository(
        db_path=creation_db, busy_timeout_ms=1_000
    ).get_runtime_resume_context_for_user_step(
        user_id="user-1",
        action_id=first.action_id,
        current_user_step_number=3 if has_older_checkpoint else 2,
    )
    assert resume_result.data is not None
    assert resume_result.data.intervening_user_step is not None
    assert resume_result.data.intervening_user_step.step_id == failed.user_step_id
    assert (resume_result.data.checkpoint_row is not None) is has_older_checkpoint


@pytest.mark.parametrize("stage", ["execute", None])
def test_submit_action_message_rejects_non_prepare_contextless_terminal(
    creation_db: Path,
    stage: str | None,
) -> None:
    failed = submit_action_message(_text_command())
    _fail_action_run(creation_db, failed, stage=stage)

    with pytest.raises(ActionMessageConflictError, match="not prepare-stage"):
        submit_action_message(_followup_command(failed, message_id="message-2"))


def test_submit_action_message_rejects_multiple_checkpoint_free_user_turns(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    _fail_action_run(creation_db, first, stage="prepare")
    second = submit_action_message(_followup_command(first, message_id="message-2"))
    _fail_action_run(creation_db, second, stage="prepare")

    with pytest.raises(ActionMessageConflictError, match="unreconstructable"):
        submit_action_message(_followup_command(second, message_id="message-3"))


def test_submit_action_message_rejects_foreign_or_nonroot_expected_process(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    other = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="message-other",
                content="Other work",
            ),
        )
    )
    _finish_initial_run(creation_db, action_id=first.action_id)

    for index, process_id in enumerate(("missing-process", other.process_id)):
        with pytest.raises(ExpectedProcessConflictError) as captured:
            submit_action_message(
                SubmitActionMessageCommand(
                    user_id="user-1",
                    target=ExistingActionTarget(
                        action_id=first.action_id,
                        expected_process_id=process_id,
                    ),
                    message=ActionUserMessageInput(
                        message_id=f"rejected-{index}",
                        content="Continue",
                    ),
                )
            )
        assert captured.value.failure_type == "ExpectedProcessConflict"

    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_action_steps "
                "WHERE user_message_id LIKE 'rejected-%'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("corruption", "error_pattern"),
    [
        ("payload", "job payload field must be non-empty string: job_id"),
        ("missing_job", "Action lineage job_id is incomplete"),
    ],
)
def test_submit_action_message_surfaces_corrupt_expected_process_envelope(
    creation_db: Path, corruption: str, error_pattern: str
) -> None:
    first = submit_action_message(_text_command())
    _finish_initial_run(creation_db, action_id=first.action_id)
    with sqlite3.connect(creation_db) as connection:
        if corruption == "payload":
            connection.execute(
                "UPDATE job_payloads SET payload_json='{}' WHERE job_id=?",
                (first.job_id,),
            )
        else:
            connection.execute("DELETE FROM jobs WHERE job_id=?", (first.job_id,))

    with pytest.raises(MigrationError, match=error_pattern):
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=first.action_id,
                    expected_process_id=first.process_id,
                ),
                message=ActionUserMessageInput(
                    message_id="message-2",
                    content="Continue",
                ),
            )
        )


def test_submit_action_message_keeps_approval_paused_run_pending(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "UPDATE agent_actions SET status='processing' WHERE action_id=?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE processes SET status='paused' WHERE process_id=?",
            (first.process_id,),
        )
        connection.execute(
            "UPDATE jobs SET status='paused' WHERE job_id=?",
            (first.job_id,),
        )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=first.process_id,
        ),
        message=ActionUserMessageInput(message_id="message-2", content="More work"),
    )

    pending = submit_action_message(command)

    with sqlite3.connect(creation_db) as connection:
        row = connection.execute(
            "SELECT expected_process_id, adopted_process_id, step_number "
            "FROM agent_action_steps WHERE step_id=?",
            (pending.user_step_id,),
        ).fetchone()
    assert pending.disposition == "pending"
    assert pending.action_status == "processing"
    assert pending.process_id is None and pending.job_id is None
    assert row == (first.process_id, None, None)


def test_submit_action_message_records_canceled_fence_without_new_run(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'canceled' WHERE action_id = ?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'canceled' WHERE logical_key = ?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE processes SET status = 'canceled' WHERE action_id = ?",
            (first.action_id,),
        )
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=first.process_id,
        ),
        message=ActionUserMessageInput(message_id="message-2", content="More work"),
    )

    result = submit_action_message(command)
    replay = submit_action_message(command)

    assert result.disposition == "not_executed"
    assert result.action_status == "canceled"
    assert result.process_id is None and result.job_id is None
    assert replay == DeferredActionMessageResult(
        "not_executed",
        first.action_id,
        "message-2",
        result.user_step_id,
        "canceled",
        None,
        None,
        False,
    )
    with sqlite3.connect(creation_db) as connection:
        row = connection.execute(
            "SELECT expected_process_id, adoption_canceled_at, adopted_process_id, "
            "step_number FROM agent_action_steps WHERE step_id=?",
            (result.user_step_id,),
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM processes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert row is not None
    assert row[0] == first.process_id
    assert row[1] is not None
    assert row[2:] == (None, None)


def test_submit_action_message_rejects_terminal_action_status_mismatch(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    with sqlite3.connect(creation_db) as connection:
        connection.execute(
            "UPDATE agent_actions SET status = 'error' WHERE action_id = ?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE logical_key = ?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE processes SET status = 'completed' WHERE action_id = ?",
            (first.action_id,),
        )

    with pytest.raises(MigrationError, match="canonical process leaf"):
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=first.action_id,
                    expected_process_id=first.process_id,
                ),
                message=ActionUserMessageInput(
                    message_id="message-2",
                    content="Try again",
                ),
            )
        )


def test_submit_action_message_rejects_suggestion_metadata_on_followup(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    _finish_initial_run(creation_db, action_id=first.action_id)
    command = SubmitActionMessageCommand(
        user_id="user-1",
        target=ExistingActionTarget(
            action_id=first.action_id,
            expected_process_id=None,
        ),
        message=ActionUserMessageInput(
            message_id="message-2",
            content="More work",
            suggestion_approval=SuggestionApprovalInput(
                suggestion_id="suggestion-1",
                approved_at="2026-08-16T01:04:00Z",
            ),
        ),
    )

    with pytest.raises(ActionMessageConflictError, match="only for a new Action"):
        submit_action_message(command)


def test_submit_action_message_rejects_message_identity_reused_for_another_action(
    creation_db: Path,
) -> None:
    first = submit_action_message(_text_command())
    second = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="message-other",
                content="Other Action",
            ),
        )
    )
    _finish_initial_run(creation_db, action_id=first.action_id)
    _finish_initial_run(creation_db, action_id=second.action_id)

    with pytest.raises(MessageIdentityConflictError, match="different Action target"):
        submit_action_message(
            SubmitActionMessageCommand(
                user_id="user-1",
                target=ExistingActionTarget(
                    action_id=second.action_id,
                    expected_process_id=None,
                ),
                message=ActionUserMessageInput(
                    message_id="message-1",
                    content="Do the work",
                    language="ja",
                ),
            )
        )


@pytest.mark.parametrize("mode", [None, "prompt_each_time", "always_allow"])
def test_initial_approval_mode_is_committed_with_creation(
    creation_db: Path, mode: ApprovalMode | None
) -> None:
    result = submit_action_message(_text_command(approval_mode=mode))
    assert (
        load_action_approval_mode(
            db_path=creation_db,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id=result.action_id,
        )
        == mode
    )
    with sqlite3.connect(creation_db) as connection:
        assert (
            connection.execute(
                "SELECT initial_approval_mode FROM agent_actions WHERE action_id = ?",
                (result.action_id,),
            ).fetchone()[0]
            == mode
        )


def test_creation_replay_preserves_later_consent_and_rejects_changed_initial_mode(
    creation_db: Path,
) -> None:
    command = _text_command(approval_mode="always_allow")
    result = submit_action_message(command)
    set_action_approval_mode(
        db_path=creation_db,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id=result.action_id,
        approval_mode="prompt_each_time",
        updated_at="2026-08-16T02:00:00.000Z",
    )
    replay = submit_action_message(command)
    assert replay.inserted is False
    assert replay.action_id == result.action_id
    for changed_mode in (None, "prompt_each_time"):
        with pytest.raises(MessageIdentityConflictError, match="initial approval mode"):
            submit_action_message(_text_command(approval_mode=changed_mode))
    assert (
        load_action_approval_mode(
            db_path=creation_db,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id=result.action_id,
        )
        == "prompt_each_time"
    )
    with sqlite3.connect(creation_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
