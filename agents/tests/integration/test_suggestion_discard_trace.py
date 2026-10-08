"""A Suggestion no session can deliver leaves none of its text in the database."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.integration.local_runtime_ws_test_fakes import load_test_prompt_config

from pantaray_agents.agents.suggestion_agent import SuggestionAgent
from pantaray_agents.agents.suggestion_agent.context_types import (
    SuggestionStableMemoryContext,
)
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    ReconsideredInsight,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceSettings,
)
from pantaray_agents.local_runtime.tooling.suggestion_research import (
    SuggestionResearchSnapshot,
)
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.suggestion_research import (
    build_mock_suggestion_research_tools,
)
from pantaray_agents.orchestration.ws import deliverable_sessions
from pantaray_agents.schema.agent.suggestion import SuggestionStructuredOutput
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.tasks.internal_jobs import suggestion as suggestion_job

USER_ID = "user-1"
DELIVERED_ANSWER = "Delivered: finish the parser review"
DISCARDED_ANSWER = "Discarded: rename the release branch"
_STABLE_MEMORY = SuggestionStableMemoryContext(
    prompt="No stable memory roots are available.",
    has_facts=False,
    has_insights=False,
)


class _OpenSession:
    def can_deliver(self) -> bool:
        return True


class _NoActivity:
    async def get_recent_activity_logs(self, *, user_id: str, limit: int):
        return RepositoryResult(data=[])

    async def get_recent_activity_summary(
        self, *, user_id: str, summary_type: str, limit: int
    ):
        return RepositoryResult(data=[])


def _rows_containing(db_path: Path, text: str) -> dict[str, int]:
    """Every ordinary table, every column: where does `text` still appear?"""
    found: dict[str, int] = {}
    with sqlite3.connect(db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'"
        ).fetchall()
        for (table,) in tables:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            for column in columns:
                (count,) = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                    f' WHERE CAST("{column}" AS TEXT) LIKE ?',
                    (f"%{text}%",),
                ).fetchone()
                if count:
                    found[f"{table}.{column}"] = count
    return found


def _prepare_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, paused_reads: list[bool]
):
    """A real SuggestionAgent and repository; `paused_reads` answers the recording gate."""
    db_path = tmp_path / "runtime.db"
    apply_migrations(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'en', '2026-09-30T00:00:00Z', '2026-09-30T00:00:00Z')",
            (USER_ID,),
        )
    repository = LocalSuggestionRepository(
        db_path=str(db_path), busy_timeout_ms=1_000, activity_repository=_NoActivity()
    )
    llm = MockLLMClient()
    monkeypatch.setattr(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        load_test_prompt_config,
    )
    agent = SuggestionAgent(
        config={"mode": "local_runtime", "llm_client": llm},
        repository=repository,
        research_tools=build_mock_suggestion_research_tools(),
        stable_memory=_STABLE_MEMORY,
    )
    agent.client = llm
    job = "pantaray_agents.tasks.internal_jobs.suggestion"
    for name, value in {
        "read_local_runtime_db_config": lambda: (db_path, 1_000),
        "reserve_suggestion_start": lambda **_kwargs: "start",
        "capture_paused_for_user": lambda **_kwargs: (
            paused_reads.pop(0) if paused_reads else False
        ),
        "list_workspace_settings": lambda **_kwargs: WorkspaceSettings(
            read_access_scope="workspace", organizations=(), projects=(), folders=()
        ),
        "build_suggestion_research_snapshot": lambda **_kwargs: (
            SuggestionResearchSnapshot(
                folders=(),
                memory_revisions={},
                stable_memory=_STABLE_MEMORY,
                commands_allowed=False,
                read_access_scope="workspace",
            )
        ),
        "read_reconsidered_insight": lambda **_kwargs: ReconsideredInsight(
            short_term_insight="The user is reviewing the parser.",
            reconsideration_reason="The review stalled.",
            source_cursor=None,
        ),
        "deps.get_suggestion_agent": AsyncMock(return_value=agent),
        "deps.get_suggestion_repository": AsyncMock(return_value=repository),
        "deps.get_user_settings_repository": AsyncMock(
            return_value=SimpleNamespace(
                get_ui_language=AsyncMock(return_value=RepositoryResult(data="en"))
            )
        ),
    }.items():
        monkeypatch.setattr(f"{job}.{name}", value)

    async def _run(suggestion_id: str, answer: str) -> None:
        llm.set_next_response(
            SuggestionStructuredOutput.model_validate(
                {
                    "has_suggestion": True,
                    "interaction_contract": "message_only",
                    "key_point": answer,
                    "suggestion_summary": "Parser review.",
                    "target_context": {
                        "organization_name": None,
                        "project_name": None,
                    },
                }
            ).model_dump()
        )
        # The other two lens runs find nothing; the selector picks the candidate.
        nothing = SuggestionStructuredOutput.model_validate(
            {
                "has_suggestion": False,
                "interaction_contract": None,
                "key_point": "",
                "suggestion_summary": None,
                "target_context": None,
            }
        ).model_dump()
        llm.queued_responses = [
            nothing,
            nothing,
            {"tool_id": "select_suggestion", "args": {"choice": 1, "reason": "it"}},
        ]
        llm.responses["default"] = answer  # what the writer call returns
        await repository.create_processing_suggestion_row(
            user_id=USER_ID, suggestion_id=suggestion_id
        )
        await suggestion_job._run_suggestion_job(
            {
                "job_id": f"job-{suggestion_id}",
                "process_id": f"process-{suggestion_id}",
                "suggestion_id": suggestion_id,
                "user_id": USER_ID,
                "enqueued_at": "2026-09-30T00:00:00Z",
                "insight_id": "insight-1",
            }
        )

    return db_path, _run


@pytest.mark.asyncio
async def test_a_discarded_suggestion_keeps_no_answer_or_run_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path, _run = _prepare_job(tmp_path, monkeypatch, paused_reads=[])
    # The control: a delivered run records its ReAct steps and its answer.
    session = _OpenSession()
    deliverable_sessions.register_deliverable_session(USER_ID, session)
    try:
        await _run("delivered", DELIVERED_ANSWER)
    finally:
        deliverable_sessions.unregister_deliverable_session(USER_ID, session)
    delivered = _rows_containing(db_path, DELIVERED_ANSWER)
    assert "agent_suggestions.answer" in delivered
    assert "agent_suggestion_run_steps.llm_response_text" in delivered
    assert "memory_revisions.inline_body" in delivered

    # Another owner's session (the previous account's) delivers nothing here.
    deliverable_sessions.register_deliverable_session("previous-owner", session)
    try:
        with caplog.at_level("INFO"):
            await _run("discarded", DISCARDED_ANSWER)
    finally:
        deliverable_sessions.unregister_deliverable_session("previous-owner", session)

    assert _rows_containing(db_path, DISCARDED_ANSWER) == {}
    assert "because no session can deliver it" in caplog.text
    assert DISCARDED_ANSWER not in caplog.text
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, answer FROM agent_suggestions WHERE suggestion_id = ?",
            ("discarded",),
        ).fetchone() == ("canceled", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("deliverable", [True, False])
async def test_recording_stopped_during_generation_keeps_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deliverable: bool
) -> None:
    # Recording is on for the preflight read and off for the one before publishing.
    db_path, _run = _prepare_job(tmp_path, monkeypatch, paused_reads=[False, True])
    session = _OpenSession()
    if deliverable:
        deliverable_sessions.register_deliverable_session(USER_ID, session)
    try:
        await _run("paused", DISCARDED_ANSWER)
    finally:
        deliverable_sessions.unregister_deliverable_session(USER_ID, session)

    assert _rows_containing(db_path, DISCARDED_ANSWER) == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, answer FROM agent_suggestions WHERE suggestion_id = ?",
            ("paused",),
        ).fetchone() == ("canceled", None)
