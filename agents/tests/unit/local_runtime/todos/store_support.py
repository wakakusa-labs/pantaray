"""Seeds for the TODO store tests: a migrated database and the Action rows a TODO reads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.todos.store import TodoClock

USER = "user-1"
T = "2026-11-01T00:00:00.000Z"
TOKYO = ZoneInfo("Asia/Tokyo")
# Wednesday 2026-11-04, noon in Tokyo; the week starts on Monday 2026-11-02.
NOW = TodoClock(now="2026-11-04T03:00:00.000Z", zone=TOKYO)


def execute(db_path: Path, script: str) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(script)


def seed_action(
    db_path: Path,
    action_id: str,
    *,
    message_id: str,
    status: str = "success",
    run: str = "completed",
    suggestion_id: str | None = None,
    reply_to: str | None = None,
    final_output: str = "**Sent** the quote.\nDetails follow.",
) -> None:
    """An Action whose one run is `run`, as process and job status."""

    a = action_id
    suggestion = f"'{suggestion_id}'" if suggestion_id else "NULL"
    user_step = 1
    script = f"""
      INSERT INTO agent_actions(action_id, user_id, suggestion_id,
        initial_user_message_id, execution_target_json, status, final_output,
        prompt_name, prompt_version, created_at, updated_at)
        VALUES ('{a}', '{USER}', {suggestion}, '{message_id}', '{{"kind":"scratch"}}',
                '{status}', '{final_output}', 'p', '1', '{T}', '{T}');
      INSERT INTO processes(process_id, user_id, kind, status, suggestion_id,
        action_id, started_at, updated_at, heartbeat_at, next_event_seq)
        VALUES ('run-{a}', '{USER}', 'action', '{run}', {suggestion}, '{a}', '{T}',
                '{T}', '{T}', 1);
      INSERT INTO jobs(job_id, user_id, job_type, process_id, status, scheduled_at)
        VALUES ('job-{a}', '{USER}', 'execute_action', 'run-{a}', '{run}', '{T}');
    """
    if reply_to is not None:
        user_step = 2
        script += f"""
          INSERT INTO agent_action_steps(step_id, action_id, user_id, step_number,
            local_step_number, short_step_id, step_type, step_name, status,
            goal_handle, llm_response_text, adopted_process_id, source_suggestion_id,
            started_at, completed_at, created_at) VALUES ('opening-{a}', '{a}', '{USER}', 1, 1, 'S-1-ASSISTANT',
            'assistant_message', 'assistant_message', 'success', 'S', 'Offer.',
            'run-{a}', '{reply_to}', '{T}', '{T}', '{T}');
        """
    script += f"""
      INSERT INTO agent_action_steps(step_id, action_id, user_id, step_number,
        local_step_number, short_step_id, step_type, step_name, status, goal_handle,
        user_message_id, user_message_json, user_request_text, accepted_sequence,
        adopted_process_id, created_at) VALUES ('user-{a}', '{a}', '{USER}',
        {user_step}, {user_step}, 'S-{user_step}-USER', 'user_request', 'user_request',
        'success', 'S', '{message_id}', '{{}}', 'request', 1, 'run-{a}', '{T}');
    """
    execute(db_path, script)


def seed_suggestion(db_path: Path, suggestion_id: str) -> None:
    execute(
        db_path,
        f"""
        INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer,
          has_suggestion, interaction_contract, created_at, updated_at)
          VALUES ('{suggestion_id}', '{USER}', 'success', 'Offer.', 1,
                  'action_offer', '{T}', '{T}');
        """,
    )


def count(db_path: Path, sql: str) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return int(connection.execute(sql).fetchone()[0])
