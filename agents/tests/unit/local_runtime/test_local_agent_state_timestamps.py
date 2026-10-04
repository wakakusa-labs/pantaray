from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.agent_state.action_repository import (
    LocalActionRepository,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.schema.agent.action import ActionAgentResponse
from pantaray_agents.schema.agent.base import StatusType

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "sug-1"
ACTIVITY_SUMMARY_ID = "summary-1"


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
                (SUGGESTION_ID, USER_ID),
            )
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id, user_id, suggestion_id, initial_user_message_id,
                    execution_target_json, status, final_output,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES (
                    'act-1', ?, ?, 'message-1', '{"kind":"scratch"}',
                    'queued', '', 'action/executing', '1.0',
                    '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z'
                )
                """,
                (USER_ID, SUGGESTION_ID),
            )
            connection.execute(
                """
                INSERT INTO activity_summaries(
                    summary_id,
                    user_id,
                    summary_type,
                    period_start,
                    period_end,
                    summary,
                    status,
                    prompt_name,
                    prompt_version,
                    source_ids,
                    created_at,
                    updated_at
                ) VALUES (
                    ?, ?, '1h',
                    '2026-03-23T23:00:00Z', '2026-03-24T00:00:00Z',
                    'summary', 'success', 'activity_summary', '1.0', '["log-1"]',
                    '2026-03-24T00:00:00Z', '2026-03-24T00:00:00Z'
                )
                """,
                (ACTIVITY_SUMMARY_ID, USER_ID),
            )
    return db_path


@pytest.mark.asyncio
async def test_local_action_repository_updates_updated_at_on_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    timestamps = iter(
        [
            ("2026-03-24T00:00:00Z", "2026-03-24T00:00:01Z"),
            ("2026-03-24T00:00:00Z", "2026-03-24T00:10:00Z"),
        ]
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.agent_state.action_repository.build_audit_timestamps",
        lambda *, created_at=None: next(timestamps),
    )

    response = ActionAgentResponse(
        action_id="act-1",
        suggestion_id=SUGGESTION_ID,
        user_id=USER_ID,
        final_output="done",
        created_at="2026-03-24T00:00:00Z",
        status=StatusType.SUCCESS,
    )
    first = await repo.save_action(
        response,
        final_prompt_text="prompt",
        prompt_name="action/executing",
        prompt_version="1.0",
    )
    second = await repo.save_action(
        response.model_copy(update={"final_output": "done-again"}),
        final_prompt_text="prompt",
        prompt_name="action/executing",
        prompt_version="1.0",
    )

    assert first.error is None
    assert second.error is None
    assert first.data is not None
    assert second.data is not None
    assert first.data["created_at"] == "2026-03-24T00:00:00Z"
    assert first.data["updated_at"] == "2026-03-24T00:00:01Z"
    assert second.data["created_at"] == "2026-03-24T00:00:00Z"
    assert second.data["updated_at"] == "2026-03-24T00:10:00Z"
