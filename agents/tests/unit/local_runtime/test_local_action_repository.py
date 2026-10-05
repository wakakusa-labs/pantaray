from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.schema.agent.action import ActionAgentResponse
from pantaray_agents.schema.agent.base import StatusType

from .local_action_repository_support import (
    ACTION_ID,
    SUGGESTION_ID,
    USER_ID,
    bootstrap_action_repository_db,
    build_action_repository,
    save_success_action,
)


@pytest.mark.asyncio
async def test_local_action_repository_save_action_round_trip(tmp_path: Path) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)

    await save_success_action(repo)

    action_result = await repo.get_action(user_id=USER_ID, action_id=ACTION_ID)

    assert action_result.error is None
    assert action_result.data is not None
    assert action_result.data["status"] == "success"
    assert action_result.data["final_output"] == "done"
    assert action_result.data["token_budget"] == 120
    assert action_result.data["total_tokens"] == 18
    assert action_result.data["generation"] == 1


@pytest.mark.asyncio
async def test_local_action_repository_rejects_non_positive_token_budget(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_action_repository_db(tmp_path)
    repo = build_action_repository(db_path)

    result = await repo.save_action(
        ActionAgentResponse(
            action_id=ACTION_ID,
            suggestion_id=SUGGESTION_ID,
            user_id=USER_ID,
            final_output="done",
            created_at="2026-03-24T00:10:00Z",
            status=StatusType.SUCCESS,
        ),
        final_prompt_text="final prompt",
        prompt_name="action/executing",
        prompt_version="1.0",
        token_budget=0,
    )

    assert result.data is None
    assert result.error == "token_budget must be a positive integer or null"
