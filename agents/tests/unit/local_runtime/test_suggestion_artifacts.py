"""The Suggestion's artifact list: stored, relayed and read back unchanged."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketState

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.storage.migrations import apply_migrations
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)
from pantaray_agents.orchestration.overlay.bootstrap import OverlayBootstrapService
from pantaray_agents.orchestration.session.store import InMemorySessionStore
from pantaray_agents.orchestration.ws import suggestion_job_timing
from pantaray_agents.orchestration.ws.handler import WSOrchestrationHandler
from pantaray_agents.orchestration.ws.suggestion_relay import LiveSuggestionProcess
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SUGGESTION_ARTIFACTS_MAX_COUNT,
    SuggestionAgentResponse,
)

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "sug-1"
TIMESTAMP = "2026-10-24T00:00:00Z"
ARTIFACTS_MIGRATION_VERSION = 131


def _response(artifacts: list[str]) -> SuggestionAgentResponse:
    return SuggestionAgentResponse(
        suggestion_id=SUGGESTION_ID,
        user_id=USER_ID,
        created_at=TIMESTAMP,
        answer="The draft is ready.",
        status=StatusType.SUCCESS,
        has_suggestion=True,
        interaction_contract="message_only",
        artifacts=artifacts,
    )


def _repository(db_path: Path) -> LocalSuggestionRepository:
    return LocalSuggestionRepository(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        activity_repository=MagicMock(),
    )


def _insert_user(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'ja', ?, ?)",
            (USER_ID, TIMESTAMP, TIMESTAMP),
        )


async def test_rows_stored_before_the_migration_read_as_no_files(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path,
        BUSY_TIMEOUT_MS,
        tuple(m for m in migrations if m.version < ARTIFACTS_MIGRATION_VERSION),
    )
    _insert_user(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, answer,"
            " has_suggestion, interaction_contract, created_at, updated_at)"
            " VALUES (?, ?, 'success', 'Earlier advice.', 1, 'message_only', ?, ?)",
            (SUGGESTION_ID, USER_ID, TIMESTAMP, TIMESTAMP),
        )

    apply_migrations(db_path, BUSY_TIMEOUT_MS, migrations)

    row = await _repository(db_path).get_suggestion(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID
    )
    assert row.data is not None
    assert row.data["artifacts_json"] == []
    with sqlite3.connect(db_path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE agent_suggestions SET artifacts_json = '{}' WHERE suggestion_id = ?",
            (SUGGESTION_ID,),
        )


@pytest.mark.parametrize(
    "artifacts",
    [
        [""],
        ["/Users/someone/.ssh/id_rsa"],
        ["../other-suggestion/draft.md"],
        ["drafts/../../escape.md"],
        ["./draft.md"],
        ["drafts//draft.md"],
        ["drafts/"],
        ["drafts\\..\\escape.md"],
        ["draft.md", "draft.md"],
        [f"draft-{index}.md" for index in range(SUGGESTION_ARTIFACTS_MAX_COUNT + 1)],
    ],
)
def test_a_path_that_could_leave_the_folder_is_refused(artifacts: list[str]) -> None:
    with pytest.raises(ValidationError):
        _response(artifacts)


@pytest.mark.parametrize("artifacts", [["drafts/reply.md", "sheet.csv"], []])
async def test_the_list_survives_save_relay_and_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifacts: list[str]
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, BUSY_TIMEOUT_MS, load_default_migrations())
    _insert_user(db_path)
    repository = _repository(db_path)
    await repository.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=TIMESTAMP
    )
    saved = await repository.save_suggestion(
        _response(artifacts), prompt_name="suggestion", prompt_version="test"
    )
    assert saved.error is None

    monkeypatch.setattr(deps, "is_mock_mode", lambda: False)
    monkeypatch.setattr(
        suggestion_job_timing, "SUGGESTION_JOB_POLL_INTERVAL_SECONDS", 0.01
    )
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    handler = WSOrchestrationHandler(
        websocket=websocket,
        session_store=InMemorySessionStore(max_age_seconds=3600),
        session_id="sess-1",
        user_id=USER_ID,
    )
    handler._get_suggestion_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    handler._get_action_state_repository = AsyncMock(return_value=repository)  # type: ignore[method-assign]
    await asyncio.wait_for(
        handler._relay_suggestion_result(
            LiveSuggestionProcess(process_id="process-1", suggestion_id=SUGGESTION_ID)
        ),
        timeout=2.0,
    )

    sent = [call.args[0] for call in websocket.send_json.call_args_list]
    completed = [m for m in sent if m.get("event") == "process_completed"]
    assert [m["data"]["artifacts"] for m in completed] == [artifacts]
    bootstrap = await OverlayBootstrapService(repository).build_for_suggestion(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID
    )
    assert bootstrap.snapshot.artifacts == artifacts
    assert json.loads(bootstrap.model_dump_json())["snapshot"]["artifacts"] == artifacts


async def test_a_list_made_invalid_after_validation_does_not_update_the_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, BUSY_TIMEOUT_MS, load_default_migrations())
    _insert_user(db_path)
    repository = _repository(db_path)
    await repository.create_processing_suggestion_row(
        user_id=USER_ID, suggestion_id=SUGGESTION_ID, created_at=TIMESTAMP
    )
    response = _response(["draft.md"])
    response.artifacts.append("../escape.md")

    saved = await repository.save_suggestion(
        response, prompt_name="suggestion", prompt_version="test"
    )

    assert saved.error is not None
    row = await repository.get_suggestion(user_id=USER_ID, suggestion_id=SUGGESTION_ID)
    assert row.data is not None
    assert row.data["status"] == "processing"
    assert row.data["artifacts_json"] == []
