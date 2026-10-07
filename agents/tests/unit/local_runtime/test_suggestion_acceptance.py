from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.action_messages import (
    StartedActionMessageResult,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    SuggestionAcceptanceRejected,
    SuggestionAcceptanceResult,
    SuggestionAccepted,
    accept_suggestion,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import SuggestionAgentResponse

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "suggestion-1"
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
OTHER_COMMAND_ID = "22222222-2222-4222-8222-222222222222"
CREATED_AT = "2026-10-08T00:00:00.000Z"


class _NoActivity:
    async def get_recent_activity_logs(self, **_kwargs: object) -> None:
        raise AssertionError("not used")

    async def get_recent_activity_summary(self, **_kwargs: object) -> None:
        raise AssertionError("not used")


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'ja', ?, ?)",
            (USER_ID, CREATED_AT, CREATED_AT),
        )
    monkeypatch.setenv("LOCAL_DB_PATH", str(path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    register_logged_out_owner(USER_ID)
    yield path
    reset_logged_out_owner()


async def _save_action_offer(db_path: Path) -> None:
    repository = LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=_NoActivity(),
    )
    await repository.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=CREATED_AT
    )
    saved = await repository.save_suggestion(
        SuggestionAgentResponse(
            suggestion_id=SUGGESTION_ID,
            user_id=USER_ID,
            created_at=CREATED_AT,
            answer="Draft the release notes?",
            thinking="thinking",
            suggestion_summary="summary",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            interaction_contract="action_offer",
        ),
        prompt_name="suggestion",
        prompt_version="react_v2",
    )
    assert saved.error is None


async def _accept(
    command_id: str, *, supplement: str | None = None
) -> SuggestionAcceptanceResult:
    return await accept_suggestion(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        command_id=command_id,
        approval_mode="prompt_each_time",
        language="ja",
        supplement=supplement,
        supplement_project_refs=(),
        images=(),
        files=(),
    )


def _action_rows(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT action_id, initial_user_message_id FROM agent_actions"
            " WHERE suggestion_id = ?",
            (SUGGESTION_ID,),
        ).fetchall()


@pytest.mark.asyncio
async def test_accepting_records_the_approval_and_creates_one_action(
    db_path: Path,
) -> None:
    await _save_action_offer(db_path)

    first = await _accept(COMMAND_ID, supplement="  Keep it short.")
    replay = await _accept(COMMAND_ID, supplement="Keep it short.")

    assert isinstance(first, SuggestionAccepted)
    assert isinstance(first.action, StartedActionMessageResult)
    assert first.action.inserted
    # The same command replays the recorded approval instead of failing.
    assert isinstance(replay, SuggestionAccepted)
    assert replay.action.action_id == first.action.action_id
    assert not replay.action.inserted
    assert replay.accepted_at == first.accepted_at
    assert _action_rows(db_path) == [(first.action.action_id, COMMAND_ID)]
    with sqlite3.connect(db_path) as connection:
        reaction = connection.execute(
            "SELECT user_reaction, action_command_id FROM agent_suggestions"
            " WHERE suggestion_id = ?",
            (SUGGESTION_ID,),
        ).fetchone()
    assert reaction == ("accepted", COMMAND_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_status", "expected_reason"),
    [("idle", "not_allowed"), ("processing", "already_processing")],
)
async def test_another_command_cannot_accept_an_accepted_suggestion_again(
    db_path: Path, action_status: str, expected_reason: str
) -> None:
    await _save_action_offer(db_path)
    assert isinstance(await _accept(COMMAND_ID), SuggestionAccepted)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_suggestions SET action_status = ? WHERE suggestion_id = ?",
            (action_status, SUGGESTION_ID),
        )

    assert await _accept(OTHER_COMMAND_ID) == SuggestionAcceptanceRejected(
        expected_reason
    )
    assert len(_action_rows(db_path)) == 1


@pytest.mark.asyncio
async def test_a_dismissed_suggestion_cannot_be_accepted(db_path: Path) -> None:
    await _save_action_offer(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE agent_suggestions SET user_reaction = 'rejected'"
            " WHERE suggestion_id = ?",
            (SUGGESTION_ID,),
        )

    assert await _accept(COMMAND_ID) == SuggestionAcceptanceRejected("not_allowed")
    assert _action_rows(db_path) == []


@pytest.mark.asyncio
async def test_another_users_suggestion_is_not_found(db_path: Path) -> None:
    await _save_action_offer(db_path)

    result = await accept_suggestion(
        user_id="user-2",
        suggestion_id=SUGGESTION_ID,
        command_id=COMMAND_ID,
        approval_mode="prompt_each_time",
        language=None,
        supplement=None,
        supplement_project_refs=(),
        images=(),
        files=(),
    )

    assert result == SuggestionAcceptanceRejected("suggestion_not_found")
    assert _action_rows(db_path) == []
