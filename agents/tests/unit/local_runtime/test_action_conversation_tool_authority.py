import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.sqlite_visibility import (
    register_action_tool_visibility_sqlite,
)
from pantaray_agents.local_runtime.action_conversation.tool_authority import (
    ActionToolOutputIntegrityError,
    load_action_tool_output_authority_in_connection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.models import (
    ExecutionSessionCreateInput,
    ToolInvocationCompletionInput,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    create_execution_session_in_connection,
    record_tool_invocation_completion,
)
from pantaray_agents.schema.agent.base import JSONValue

from .migrated_db import prepare_test_database

TIMESTAMP = "2026-08-30T00:00:00Z"
INVOCATION_COMPLETED_AT = "2026-08-29T23:59:59Z"


def _insert_action(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> None:
    ensure_user_row(connection, user_id=user_id, timestamp=TIMESTAMP)
    connection.execute(
        """INSERT INTO agent_actions(
               action_id,user_id,initial_user_message_id,execution_target_json,
               status,final_output,prompt_name,prompt_version,created_at,updated_at
           ) VALUES (?,?,?,'{"kind":"scratch"}','processing','','test','1',?,?)""",
        (action_id, user_id, f"message-{action_id}", TIMESTAMP, TIMESTAMP),
    )


def _insert_running_invocation(
    connection: sqlite3.Connection,
    *,
    invocation_id: str,
    step_id: str,
) -> None:
    connection.execute(
        """INSERT INTO tool_invocations(
               invocation_id,user_id,action_id,step_id,tool_id,
               execution_session_id,intent_class,started_at,completed_at,status
           ) VALUES (?, 'user-1', 'action-1', ?, 'read',
                     'session-test', 'read_only', ?, NULL, 'running')""",
        (invocation_id, step_id, TIMESTAMP),
    )


def test_tool_output_authority_uses_v89_owner_visibility_and_formal_output(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    formal: dict[str, JSONValue] = {
        "schema_version": 1,
        "status": "success",
        "output": {"content": "done"},
        "output_storage_kind": "inline_json",
        "output_owner_kind": "tool_invocation",
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        register_action_tool_visibility_sqlite(connection)
        _insert_action(connection, user_id="user-1", action_id="action-1")
        _insert_action(connection, user_id="user-2", action_id="action-2")
        create_execution_session_in_connection(
            connection=connection,
            session=ExecutionSessionCreateInput(
                execution_session_id="session-test",
                user_id="user-1",
                action_id="action-1",
                parent_execution_session_id=None,
                exec_mode="brokered_file_ops",
                cwd_path=str(tmp_path),
                action_temp_dir=None,
                app_runtime_python=None,
                network_policy="cloud-proxy-only",
                read_access_scope="workspace",
                capability_snapshot_json={"allowed_capabilities": ["scoped_read"]},
                tool_allowlist_json=["read"],
                status="running",
                started_at=TIMESTAMP,
                expires_at=None,
            ),
        )
        connection.executemany(
            """INSERT INTO agent_action_steps(
                   step_id,action_id,user_id,step_number,step_type,step_name,status,
                   tool_output,started_at,completed_at,created_at
               ) VALUES (?,?,?,1,'tool_execution',?,?,?,?,?,?)""",
            (
                (
                    "visible",
                    "action-1",
                    "user-1",
                    "tool::read",
                    "success",
                    json.dumps(formal),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
                (
                    "hidden",
                    "action-1",
                    "user-1",
                    "tool::submit_final_answer",
                    "success",
                    json.dumps(formal),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
                (
                    "processing",
                    "action-1",
                    "user-1",
                    "tool::bash",
                    "processing",
                    json.dumps({**formal, "status": "processing"}),
                    TIMESTAMP,
                    None,
                    TIMESTAMP,
                ),
                (
                    "action-step-owned",
                    "action-1",
                    "user-1",
                    "tool::synthetic",
                    "success",
                    json.dumps({**formal, "output_owner_kind": "action_step"}),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
                (
                    "other-owner",
                    "action-2",
                    "user-2",
                    "tool::read",
                    "success",
                    json.dumps(formal),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
                (
                    "unlinked",
                    "action-1",
                    "user-1",
                    "tool::read",
                    "success",
                    json.dumps(formal),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
                (
                    "mismatch",
                    "action-1",
                    "user-1",
                    "tool::read",
                    "success",
                    json.dumps(formal),
                    TIMESTAMP,
                    TIMESTAMP,
                    TIMESTAMP,
                ),
            ),
        )
        _insert_running_invocation(
            connection,
            invocation_id="invocation-visible",
            step_id="visible",
        )
        _insert_running_invocation(
            connection,
            invocation_id="invocation-mismatch",
            step_id="mismatch",
        )

    invocation_outputs: tuple[tuple[str, JSONValue], ...] = (
        ("invocation-visible", formal["output"]),
        ("invocation-mismatch", {"content": "different"}),
    )
    for invocation_id, output in invocation_outputs:
        record_tool_invocation_completion(
            db_path=db_path,
            busy_timeout_ms=1_000,
            completion=ToolInvocationCompletionInput(
                invocation_id=invocation_id,
                status="completed",
                completed_at=INVOCATION_COMPLETED_AT,
                output_json=output,
                output_storage_kind="inline_json",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
                redaction_applied=False,
            ),
        )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        register_action_tool_visibility_sqlite(connection)
        authority = load_action_tool_output_authority_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            step_id="visible",
        )
        assert authority is not None
        assert (
            authority.tool_id,
            authority.output.output,
            authority.output_owner_id,
        ) == (
            "read",
            {"content": "done"},
            "invocation-visible",
        )
        action_step_authority = load_action_tool_output_authority_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            step_id="action-step-owned",
        )
        assert action_step_authority is not None
        assert action_step_authority.output_owner_id == "action-step-owned"
        for step_id in ("hidden", "processing", "other-owner", "missing"):
            assert (
                load_action_tool_output_authority_in_connection(
                    connection=connection,
                    user_id="user-1",
                    action_id="action-1",
                    step_id=step_id,
                )
                is None
            )
        with pytest.raises(ActionToolOutputIntegrityError):
            load_action_tool_output_authority_in_connection(
                connection=connection,
                user_id="user-1",
                action_id="action-1",
                step_id="unlinked",
            )
        with pytest.raises(ActionToolOutputIntegrityError):
            load_action_tool_output_authority_in_connection(
                connection=connection,
                user_id="user-1",
                action_id="action-1",
                step_id="mismatch",
            )
        connection.execute(
            "UPDATE agent_action_steps SET status='error', tool_output=? "
            "WHERE step_id='mismatch'",
            (
                json.dumps(
                    {**formal, "status": "error", "output": {"content": "different"}}
                ),
            ),
        )
        error_authority = load_action_tool_output_authority_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            step_id="mismatch",
        )
        assert error_authority is not None
        assert error_authority.output_owner_id == "invocation-mismatch"
        for broken_output in (
            '{"status":"success"}',
            json.dumps({**formal, "status": "error"}),
        ):
            connection.execute(
                "UPDATE agent_action_steps SET tool_output=? WHERE step_id='unlinked'",
                (broken_output,),
            )
            with pytest.raises(ActionToolOutputIntegrityError):
                load_action_tool_output_authority_in_connection(
                    connection=connection,
                    user_id="user-1",
                    action_id="action-1",
                    step_id="unlinked",
                )
