"""An Action created from a Suggestion continues from its own copy of the files."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    NewActionTarget,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    SuggestionAccepted,
    accept_suggestion,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
    resolve_managed_action_workspace_root,
    resolve_suggestion_artifacts_root,
)
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentResponse,
    SuggestionInteractionContract,
)

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
SUGGESTION_ID = "suggestion-1"
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
CREATED_AT = "2026-10-24T00:00:00.000Z"
LISTED = ["report.md", "drafts/plan.md", "escape.md"]


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


async def _save_suggestion(
    db_path: Path, *, contract: SuggestionInteractionContract, artifacts: list[str]
) -> Path:
    """Save a Suggestion and write its folder: the listed files, one of them a
    symlink to a file outside, and a file it does not list."""

    folder = resolve_suggestion_artifacts_root(
        db_path=db_path, user_id=USER_ID, suggestion_id=SUGGESTION_ID
    )
    (folder / "drafts").mkdir(parents=True)
    (folder / "report.md").write_text("# Report")
    (folder / "drafts/plan.md").write_text("plan")
    (folder / "scratch.txt").write_text("not listed")
    outside = db_path.parent / "private.txt"
    outside.write_text("outside the folder")
    (folder / "escape.md").symlink_to(outside)
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
            answer="I drafted the report.",
            thinking="thinking",
            suggestion_summary="summary",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            interaction_contract=contract,
            artifacts=artifacts,
        ),
        prompt_name="suggestion",
        prompt_version="react_v2",
    )
    assert saved.error is None
    return folder


async def _accept() -> SuggestionAccepted:
    result = await accept_suggestion(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        command_id=COMMAND_ID,
        approval_mode="prompt_each_time",
        language="en",
        supplement=None,
        supplement_project_refs=(),
        supplement_work_refs=(),
        images=(),
        files=(),
    )
    assert isinstance(result, SuggestionAccepted)
    return result


def _reply_command() -> SubmitActionMessageCommand:
    return SubmitActionMessageCommand(
        user_id=USER_ID,
        target=NewActionTarget(reply_to_suggestion_id=SUGGESTION_ID),
        message=ActionUserMessageInput(message_id=COMMAND_ID, content="Shorter."),
    )


def _workspace(db_path: Path, action_id: str) -> Path:
    return resolve_action_storage_paths(
        db_path=db_path, user_id=USER_ID, action_id=action_id
    ).workspace


def _workspace_files(workspace: Path) -> dict[str, str]:
    return {
        str(path.relative_to(workspace)): path.read_text()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() or path.is_symlink()
    }


def _first_request_text(db_path: Path, action_id: str) -> str:
    with sqlite3.connect(db_path) as connection:
        (text,) = connection.execute(
            "SELECT user_request_text FROM agent_action_steps"
            " WHERE action_id = ? AND step_type = 'user_request'",
            (action_id,),
        ).fetchone()
    return str(text)


def _assert_continues_from_files(db_path: Path, action_id: str) -> None:
    assert _workspace_files(_workspace(db_path, action_id)) == {
        "suggestion/report.md": "# Report",
        "suggestion/drafts/plan.md": "plan",
    }
    text = _first_request_text(db_path, action_id)
    assert "Files from the suggestion:" in text
    assert "- suggestion/report.md\n- suggestion/drafts/plan.md" in text
    assert "escape.md" not in text


@pytest.mark.asyncio
async def test_accepting_copies_the_listed_files_and_names_them(
    db_path: Path,
) -> None:
    folder = await _save_suggestion(db_path, contract="action_offer", artifacts=LISTED)

    first = await _accept()

    action_id = first.action.action_id
    _assert_continues_from_files(db_path, action_id)
    # The Action's copy outlives the Suggestion's folder, and the same command
    # still replays the Action it created.
    shutil.rmtree(folder)
    replay = await _accept()
    assert replay.action.action_id == action_id
    assert not replay.action.inserted
    _assert_continues_from_files(db_path, action_id)


@pytest.mark.asyncio
async def test_replying_copies_the_listed_files_and_names_them(
    db_path: Path,
) -> None:
    await _save_suggestion(db_path, contract="message_only", artifacts=LISTED)

    result = submit_action_message(_reply_command())

    _assert_continues_from_files(db_path, result.action_id)
    assert submit_action_message(_reply_command()).action_id == result.action_id


@pytest.mark.asyncio
async def test_a_suggestion_without_files_starts_the_action_as_before(
    db_path: Path,
) -> None:
    await _save_suggestion(db_path, contract="action_offer", artifacts=[])

    action_id = (await _accept()).action.action_id

    assert _workspace_files(_workspace(db_path, action_id)) == {}
    assert "Files from the suggestion" not in _first_request_text(db_path, action_id)


@pytest.mark.asyncio
async def test_a_refused_reply_leaves_no_copied_files(db_path: Path) -> None:
    # Replying in words is not how an offer the user has not dismissed is answered.
    await _save_suggestion(db_path, contract="action_offer", artifacts=LISTED)

    with pytest.raises(ActionMessageConflictError):
        submit_action_message(_reply_command())

    workspaces = resolve_managed_action_workspace_root(db_path=db_path)
    assert not list(workspaces.rglob("suggestion"))
