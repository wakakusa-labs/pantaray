"""The tool call identity migration must not disturb an installed store.

These use the production migration list rather than `support.load_default_migrations`,
which stops below the reset version and would never reach this migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action

from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)

BUSY_TIMEOUT_MS = 5_000
MIGRATION_VERSION = 114
TIMESTAMP = "2026-09-19T00:00:00Z"


def _store_with_a_finished_turn(tmp_path: Path) -> Path:
    """A store holding one adopted USER, its THINK, and two batched tool rows."""

    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=tuple(
            migration
            for migration in load_default_migrations()
            if migration.version < MIGRATION_VERSION
        ),
    )
    insert_agent_action(db_path=db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute(
                """INSERT INTO processes(
                    process_id, user_id, action_id, kind, status, started_at,
                    updated_at, heartbeat_at, next_event_seq
                ) VALUES ('run-1','user-1','action-1','action','running',?,?,?,1)""",
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                """INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, local_step_number,
                    short_step_id, step_type, step_name, status, goal_handle,
                    user_request_text, accepted_sequence, adopted_process_id,
                    created_at
                ) VALUES ('user-step','action-1','user-1',1,1,'S-1-USER',
                    'user_request','user_request','success','S','依頼',1,'run-1',?)""",
                (TIMESTAMP,),
            )
            connection.execute(
                """INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, parent_step_id, step_number,
                    local_step_number, short_step_id, step_type, step_name, status,
                    goal_handle, llm_response_text, runtime_state_checkpoint,
                    runtime_state_checkpoint_version, created_at
                ) VALUES ('think-step','action-1','user-1','user-step',2,2,'S-2-THINK',
                    'llm_output','supervisor_think','success','S','{"calls":2}',
                    '{"history_by_scope":{}}',4,?)""",
                (TIMESTAMP,),
            )
            for index, (step_id, short_step_id) in enumerate(
                (("tool-step-a", "S-2-TOOL"), ("tool-step-b", "S-3-TOOL")), start=2
            ):
                connection.execute(
                    """INSERT INTO agent_action_steps(
                        step_id, action_id, user_id, parent_step_id, step_number,
                        local_step_number, short_step_id, step_type, step_name,
                        status, goal_handle, tool_args, tool_output, created_at
                    ) VALUES (?,'action-1','user-1','think-step',?,?,?,
                        'tool_execution','tool::read','success','S',
                        '{"tool_id":"read"}','{"status":"success"}',?)""",
                    (step_id, index, index, short_step_id, TIMESTAMP),
                )
    return db_path


def test_migration_keeps_existing_steps_and_their_references(tmp_path: Path) -> None:
    db_path = _store_with_a_finished_turn(tmp_path)
    with sqlite3.connect(db_path) as connection:
        before = connection.execute(
            "SELECT * FROM agent_action_steps ORDER BY step_id"
        ).fetchall()
        original_columns = ",".join(
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_action_steps)")
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )

    # A violation here is unrecoverable in the field: the schema version has
    # already advanced, so the migration never runs again and every later start
    # fails on the same check.
    verify_database_integrity(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    with sqlite3.connect(db_path) as connection:
        after = connection.execute(
            f"SELECT {original_columns} FROM agent_action_steps ORDER BY step_id"
        ).fetchall()
        added = connection.execute(
            """SELECT call_id, llm_step_id, provider_turn
            FROM agent_action_steps ORDER BY step_id"""
        ).fetchall()
    assert after == before
    assert added == [(None, None, None)] * 4


def test_migrated_store_accepts_the_identity_of_a_batched_turn(
    tmp_path: Path,
) -> None:
    db_path = _store_with_a_finished_turn(tmp_path)
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        with connection:
            for step_id, call_id in (
                ("tool-step-a", "call_a"),
                ("tool-step-b", "call_b"),
            ):
                connection.execute(
                    """UPDATE agent_action_steps
                    SET call_id = ?, llm_step_id = 'think-step' WHERE step_id = ?""",
                    (call_id, step_id),
                )
            connection.execute(
                """UPDATE agent_action_steps
                SET provider_turn = '{"provider":"openai","items":[{"type":"reasoning"}]}',
                    provider_turn_identity = 'api_key:openai:gpt-5.6-luna:abc'
                WHERE step_id = 'think-step'"""
            )
        grouped = connection.execute(
            """SELECT call_id FROM agent_action_steps
            WHERE llm_step_id = 'think-step' ORDER BY step_number"""
        ).fetchall()
    assert grouped == [("call_a",), ("call_b",)]
    verify_database_integrity(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)


@pytest.mark.parametrize(
    "assignment",
    [
        # Identity belongs to the executed call, not to the step that declared it.
        "call_id = 'call_a', llm_step_id = 'think-step' WHERE step_id = 'think-step'",
        # A call_id without its declaring step cannot be grouped into a turn.
        "call_id = 'call_a' WHERE step_id = 'tool-step-a'",
        "llm_step_id = 'think-step' WHERE step_id = 'tool-step-a'",
        # The opaque turn belongs to the row that produced it.
        "provider_turn = '{\"provider\":\"openai\"}' WHERE step_id = 'tool-step-a'",
        "provider_turn = 'not json' WHERE step_id = 'think-step'",
        # A turn is readable only by the account that issued it, so it is never
        # stored without one, and an identity names no turn on its own.
        "provider_turn = '{\"provider\":\"openai\"}' WHERE step_id = 'think-step'",
        "provider_turn_identity = 'api_key:openai:m:abc' WHERE step_id = 'think-step'",
        # A prefix fingerprint describes a stored turn and nothing else.
        "provider_turn_fingerprint = 'fp' WHERE step_id = 'think-step'",
    ],
)
def test_migrated_store_rejects_misplaced_identity(
    tmp_path: Path, assignment: str
) -> None:
    db_path = _store_with_a_finished_turn(tmp_path)
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE agent_action_steps SET {assignment}")
