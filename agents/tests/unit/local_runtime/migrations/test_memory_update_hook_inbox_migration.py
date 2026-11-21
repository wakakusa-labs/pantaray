"""Pending Memory update work survives the move onto the builtin.memory_update hook."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from ..memory_update_hook import Tick, finish_memory_jobs, memory_payloads, tick
from .support import _insert_user, _migrations_before, apply_migrations

__all__ = ["tick"]

MIGRATION_NAME = "0149_memory_update_hook_inbox.sql"
AT = "2026-11-20T10:00:00.000Z"
LATER = "2026-11-20T10:05:00.000Z"


def _message(connection: sqlite3.Connection, text: str) -> int:
    cursor = connection.execute(
        """INSERT INTO chat_items(item_id, user_id, message_id, kind, payload, created_at)
           VALUES (?, 'user-1', ?, 'user_message', ?, ?)""",
        (
            f"item-{text}",
            f"message-{text}",
            json.dumps(
                {
                    "kind": "user_message",
                    "text": text,
                    "quote_item_id": None,
                    "images": [],
                    "files": [],
                }
            ),
            AT,
        ),
    )
    return int(cursor.lastrowid or 0)


def _dismissal(connection: sqlite3.Connection, suggestion_id: str, at: str) -> None:
    connection.execute(
        """INSERT INTO agent_suggestions(
               suggestion_id, user_id, status, answer, prompt_name, prompt_version,
               has_suggestion, interaction_contract, user_reaction, rejected_at,
               action_status, created_at, updated_at
           ) VALUES (?, 'user-1', 'success', 'Shall I?', 'suggestion', '1', 1,
                     'action_offer', 'rejected', ?, 'idle', ?, ?)""",
        (suggestion_id, at, at, at),
    )


def _seed_pre_upgrade(connection: sqlite3.Connection) -> int:
    """Each source with work handed and work still pending; returns the last
    chat sequence already handed."""
    _insert_user(connection, "user-1")
    connection.executemany(
        """INSERT INTO memory_agent_triggers(
               user_id, trigger_kind, source_id, status, outcome_code, handled_at,
               created_at
           ) VALUES ('user-1', ?, ?, ?, ?, ?, ?)""",
        [
            ("memory_from_short_insight", "insight-pending", "pending", None, None, AT),
            ("memory_from_24h_summary", "summary-pending", "pending", None, None, AT),
            ("memory_from_short_insight", "insight-done", "skipped", "X", AT, AT),
        ],
    )
    connection.execute(
        """INSERT INTO memory_agent_triggers(
               user_id, trigger_kind, source_id, status, created_at, action_id,
               action_completed_at, turn_start_step_number, turn_end_step_number,
               action_prompt_name, action_prompt_version
           ) VALUES ('user-1', 'memory_from_action_terminal', 'action-1:4',
                     'pending', ?, 'action-1', ?, 2, 4, 'action', '1.0')""",
        (AT, AT),
    )
    handed = _message(connection, "handed")
    _message(connection, "pending")
    connection.execute(
        """INSERT INTO memory_chat_cursors(user_id, through_sequence, updated_at)
           VALUES ('user-1', ?, ?)""",
        (handed, AT),
    )
    _dismissal(connection, "s-handed", AT)
    _dismissal(connection, "s-pending", LATER)
    _dismissal(connection, "s-replied", LATER)
    connection.execute(
        """INSERT INTO memory_dismissal_cursors(
               user_id, through_rejected_at, through_suggestion_id, updated_at
           ) VALUES ('user-1', ?, 's-handed', ?)""",
        (AT, AT),
    )
    connection.execute(
        """INSERT INTO processes(process_id, user_id, kind, status, action_id,
               next_event_seq, started_at, updated_at, heartbeat_at)
           VALUES ('process-1', 'user-1', 'action', 'running', 'action-2', 1,
                   ?, ?, ?)""",
        (AT, AT, AT),
    )
    connection.execute(
        """INSERT INTO agent_action_steps(
               step_id, action_id, user_id, step_number, local_step_number,
               short_step_id, step_type, step_name, status, goal_handle,
               retry_count, prompt_tokens, completion_tokens, llm_response_text,
               adopted_process_id, source_suggestion_id, started_at, completed_at,
               created_at
           ) VALUES ('step-1', 'action-2', 'user-1', 1, 1, 'S-1-ASSISTANT',
                     'assistant_message', 'assistant_message', 'success', 'S',
                     0, 0, 0, 'offer', 'process-1', 's-replied', ?, ?, ?)""",
        (LATER, LATER, LATER),
    )
    return handed


def test_work_pending_before_the_upgrade_reaches_the_first_run_once(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches pending triggers, chat after the cursor or unanswered dismissals
    # lost across the upgrade, and work the dispatcher had already handed (or a
    # dismissal the user answered) handed again.
    path = tmp_path / "runtime.sqlite3"
    migrations = load_default_migrations()
    apply_migrations(path, 1000, _migrations_before(migrations, MIGRATION_NAME))
    with closing(sqlite3.connect(path)) as connection, connection:
        handed = _seed_pre_upgrade(connection)
    apply_migrations(path, 1000, migrations)

    now = datetime(2026, 11, 20, 10, 6, tzinfo=UTC)
    tick(path, now)
    finish_memory_jobs(path)
    tick(path, now + timedelta(hours=1))

    (payload,) = memory_payloads(path)
    assert payload["short_insight_ids"] == ["insight-pending"]
    assert payload["summary_ids"] == ["summary-pending"]
    assert [turn["source_id"] for turn in payload["action_terminals"]] == ["action-1:4"]
    assert payload["chat"] == {
        "after_sequence": handed,
        "through_sequence": handed + 1,
    }
    assert payload.get("dismissed_suggestion_ids") == ["s-pending"]
    with closing(sqlite3.connect(path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert not tables & {
        "memory_agent_triggers",
        "memory_chat_cursors",
        "memory_dismissal_cursors",
    }
